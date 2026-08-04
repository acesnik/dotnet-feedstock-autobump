#!/usr/bin/env python3
"""Turn discovery output into a plan: what to bump, and what to escalate.

The organising principle: **mechanical changes become PRs, judgment calls become
issues.**

A patch bump inside a tracked release line is mechanical -- two version variables
and a hash per platform, all derivable from Microsoft's metadata. Each tracked
line gets its own PR against its own branch, so concurrent bumps never conflict.

Everything else needs a human:

* A line newer than anything tracked appearing. Adopting it is a policy call
  (LTS vs STS), not a data question.
* A still-supported line that isn't tracked at all -- a maintenance gap. 9.0 is
  exactly this today: published as 9.0.203 from main, then 10.0 took over main
  and no v9 branch was ever cut, leaving it orphaned with nowhere to patch from.
* Microsoft starting to publish a new architecture.
* Microsoft *dropping* an architecture the recipe packages -- impending
  breakage, since the next bump would fail outright.

EOL lines are ignored by design. They sit at their final released versions and
Microsoft will publish no more, so there is nothing to detect.

Usage:
    plan.py channels.json path/to/feedstock-checkout > plan.json

The checkout needs all tracked branches available as refs (fetch-depth: 0);
recipes are read with `git show <ref>:recipe/meta.yaml` rather than from the
working tree, so nothing has to be checked out per line.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
REPODATA = "https://conda.anaconda.org/conda-forge/{subdir}/repodata.json"
USER_AGENT = "dotnet-feedstock-autobump"
SUPPORTED = ("active", "maintenance")


def load_updater():
    """Import the updater so PLATFORMS is read, never duplicated.

    The set of RIDs the recipe packages lives in exactly one place. Copying it
    here would let the audit drift from what the recipe actually does, which is
    the specific failure this is meant to detect.
    """
    spec = importlib.util.spec_from_file_location(
        "updater", HERE / "update-dotnet-version.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def strip_comments(obj):
    """Drop the `_*_comment` keys channels.json uses for documentation."""
    if isinstance(obj, dict):
        return {k: strip_comments(v) for k, v in obj.items() if not k.startswith("_")}
    return obj


def git_show(repo: Path, ref: str, path: str) -> str | None:
    """Read a file at a ref without checking it out. None if the ref is absent.

    Remote-tracking refs are tried FIRST and the bare ref last. That ordering is
    deliberate: trying the bare ref first let a stale *local* branch shadow the
    remote one, which silently reported upstream v8 as 8.0.407 when it was
    actually at 8.0.408 -- wrong data, no error, in a tool whose entire job is
    reading other people's branches.

    A CI checkout names the remote `origin`, but a developer's clone may not
    (this feedstock's upstream is called `originDoNotPushHere`), so all remotes
    are searched before falling back.
    """
    candidates = [f"origin/{ref}", f"upstream/{ref}"]
    remotes = subprocess.run(
        ["git", "-C", str(repo), "remote"], capture_output=True, text=True
    )
    if remotes.returncode == 0:
        candidates += [f"{r}/{ref}" for r in remotes.stdout.split()]
    # Remote-tracking refs may also live under a custom refspec namespace.
    allrefs = subprocess.run(
        ["git", "-C", str(repo), "for-each-ref", "--format=%(refname:short)",
         "refs/remotes"],
        capture_output=True, text=True,
    )
    if allrefs.returncode == 0:
        candidates += [r for r in allrefs.stdout.split() if r.endswith(f"/{ref}")]
    # Bare ref last -- see the note above.
    candidates.append(ref)

    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        r = subprocess.run(
            ["git", "-C", str(repo), "show", f"{candidate}:{path}"],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            return r.stdout
    return None


def recipe_versions(text: str) -> tuple[str, str]:
    sdk = re.search(r'set\s+sdk_version\s*=\s*"([^"]*)"', text)
    rt = re.search(r'set\s+runtime_version\s*=\s*"([^"]*)"', text)
    return (sdk.group(1) if sdk else "?", rt.group(1) if rt else "?")


def repodata_size(subdir: str) -> int | None:
    """Content-Length of a subdir's repodata.json via HEAD. None if absent.

    A cheap proxy for "does this subdir have an ecosystem". Counting packages
    properly would mean downloading linux-64's ~432 MB repodata, which CI can't
    do. Heuristic, not a guarantee -- and see active_subdirs in channels.json for
    the failure mode it does not catch.
    """
    req = urllib.request.Request(
        REPODATA.format(subdir=subdir),
        method="HEAD",
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            n = resp.headers.get("Content-Length")
            return int(n) if n else None
    except (urllib.error.HTTPError, urllib.error.URLError):
        return None


def ckey(ch: str):
    return [int(x) if x.isdigit() else 0 for x in ch.split(".")]


def plan_lines(cfg, channels, repo: Path, updater=None):
    """Per-line bumps, plus escalations about which lines exist at all."""
    # Loaded lazily so callers (and tests) can pass three arguments; the version
    # rules live in the updater so there is one definition, not two.
    if updater is None:
        updater = load_updater()
    tracked: dict[str, str] = cfg.get("tracked", {})
    policy = cfg.get("policy", "manual")
    by_ch = {c["channel"]: c for c in channels}
    bumps, issues, lines, notices = [], [], [], []
    transition = None
    transition_cfg = cfg.get("transition", {})

    newest_tracked = max(tracked, key=ckey) if tracked else None

    for ch, branch in sorted(tracked.items(), key=lambda kv: ckey(kv[0])):
        meta = git_show(repo, branch, "recipe/meta.yaml")
        info = by_ch.get(ch)
        if meta is None:
            issues.append(
                {
                    "key": f"missing-branch-{branch}",
                    "title": f"Tracked branch `{branch}` for .NET {ch} does not exist",
                    "body": (
                        f"`channels.json` maps **{ch}** to branch `{branch}`, but "
                        "that ref isn't present in the feedstock checkout.\n\n"
                        "Either the branch was deleted or `tracked` is wrong. "
                        f"Nothing can be bumped for {ch} until this is resolved."
                    ),
                }
            )
            continue
        cur_sdk, cur_rt = recipe_versions(meta)
        if info is None:
            issues.append(
                {
                    "key": f"channel-gone-{ch}",
                    "title": f".NET {ch} is no longer in Microsoft's release index",
                    "body": (
                        f"`{branch}` ships **{cur_sdk}** for line {ch}, but {ch} is "
                        "absent from `releases-index.json`. Likely removed after "
                        "EOL; consider dropping it from `tracked`."
                    ),
                }
            )
            continue

        # A vN branch whose recipe carries a different line's version was almost
        # certainly cut from the wrong commit -- e.g. branching v9 off a main that
        # had already moved to 10.0. A notice rather than an issue, because it is
        # often benign: the recipe is version-agnostic (`framework` derives from
        # sdk_version), so the bump still produces a correct package, and a branch
        # cut from a newer main inherits newer rerender infrastructure.
        if cur_sdk != "?" and not cur_sdk.startswith(ch.split(".")[0] + "."):
            notices.append(
                f"`{branch}` is tracked for {ch} but its recipe says {cur_sdk} — "
                f"cut from a {'.'.join(cur_sdk.split('.')[:2])} commit. Harmless "
                "if intentional (the recipe is version-agnostic), but check it is "
                "not the wrong branch."
            )

        phase = info["support_phase"]
        entry = {
            "channel": ch,
            "branch": branch,
            "current_sdk": cur_sdk,
            "current_runtime": cur_rt,
            "latest_sdk": info["latest_sdk"],
            "support_phase": phase,
            "stale": info["latest_sdk"] != cur_sdk,
        }
        lines.append(entry)

        if phase == "eol":
            # EOL lines are not watched, but one already in `tracked` that has
            # *just* gone EOL still deserves its final bump -- the #99/#100/#103
            # pattern -- before being dropped.
            if entry["stale"]:
                issues.append(
                    {
                        "key": f"eol-final-bump-{ch}",
                        "title": f".NET {ch} reached EOL at {info['latest_sdk']}; `{branch}` has {cur_sdk}",
                        "body": (
                            f"Line **{ch}** is now `eol`. Its final release is "
                            f"`{info['latest_sdk']}` but `{branch}` still ships "
                            f"`{cur_sdk}`.\n\n"
                            "The convention here is a final \"update to end of life "
                            "version\" PR (conda-forge/dotnet-feedstock#99, #100, "
                            f"#103), then removing {ch} from `tracked`."
                        ),
                    }
                )
            continue

        if not entry["stale"]:
            continue

        # A preview line must never be bumped automatically. Two independent
        # reasons, both verified: conda-build refuses a hyphenated version, and
        # even mangled it would outrank stable (10.0.302 < 11.0.100-preview.6),
        # so `conda install dotnet` would resolve to a preview.
        if phase == "preview":
            issues.append(
                {
                    "key": f"preview-tracked-{ch}",
                    "title": f".NET {ch} is tracked but is still a preview -- refusing to bump",
                    "body": (
                        f"`channels.json` tracks **{ch}** on `{branch}`, but "
                        f"Microsoft lists it as `preview` with SDK "
                        f"`{info['latest_sdk']}`. No PR was opened, for two "
                        "reasons:\n\n"
                        "1. conda-build rejects the version outright — "
                        "`Bad character(s) (-) in package/version`. Package "
                        "filenames are `name-version-build`, so a hyphen in a "
                        "version is ambiguous, and every Microsoft preview SDK "
                        "string contains one.\n"
                        f"2. Even with the version mangled, a preview sorts above "
                        f"stable: `10.0.302 < {info['latest_sdk']}`. Publishing it "
                        "would make `conda install dotnet` resolve to a preview "
                        "build.\n\n"
                        "Shipping previews would need a deliberate scheme — a "
                        "mangled version *and* a separate package name or channel "
                        f"label — not a version bump. Remove {ch} from `tracked` "
                        "until it reaches `active`."
                    ),
                }
            )
            continue

        problem = updater.conda_version_problem(info["latest_sdk"])
        if problem:
            issues.append(
                {
                    "key": f"unpackageable-version-{ch}-{info['latest_sdk']}",
                    "title": f".NET {ch} latest SDK `{info['latest_sdk']}` is not a valid conda version",
                    "body": (
                        f"Line **{ch}** has `{info['latest_sdk']}` upstream, but "
                        f"{problem}. No PR was opened; it could not build.\n\n"
                        "This guard is deliberately general rather than a "
                        "preview-only check, so any unexpected version string "
                        "escalates instead of producing a broken PR."
                    ),
                }
            )
            continue

        bumps.append(entry)

    # Lines Microsoft still supports that we don't track at all.
    for ch, info in sorted(by_ch.items(), key=lambda kv: ckey(kv[0])):
        if ch in tracked or info["support_phase"] not in SUPPORTED:
            continue
        if newest_tracked and ckey(ch) > ckey(newest_tracked):
            # Newer than everything tracked: a policy decision, gated below.
            continue
        issues.append(
            {
                "key": f"untracked-supported-{ch}",
                "title": f".NET {ch} is still supported but no branch tracks it",
                "body": (
                    f"Microsoft lists **{ch}** as `{info['support_phase']}` "
                    f"(`{info['release_type']}`), latest SDK "
                    f"`{info['latest_sdk']}`, but `channels.json` has no branch "
                    f"for it, so it cannot receive patches.\n\n"
                    "This is how a line gets orphaned: it ships from `main`, a "
                    "newer line takes `main` over, and no `vN` branch is cut on "
                    "the way past. conda-forge keeps serving the last version "
                    "published, indefinitely, with no way to update it.\n\n"
                    f"To fix: cut a `v{ch.split('.')[0]}` branch from the commit "
                    f"where `main` last carried a {ch} recipe, then add "
                    f"`\"{ch}\": \"v{ch.split('.')[0]}\"` to `tracked`."
                ),
            }
        )

    # Newer-than-tracked candidate lines, judged against declared policy.
    #
    # Anything not escalated here still produces a NOTICE. Silence was the
    # original sin of this workflow -- a hardcoded channel list that would let
    # .NET 11 and 12 ship unremarked. A notice costs a line in the run summary
    # and never opens anything, but it means "we saw it and chose not to act" is
    # visible rather than indistinguishable from "we never looked".
    if newest_tracked:
        for ch, info in sorted(by_ch.items(), key=lambda kv: ckey(kv[0])):
            if ckey(ch) <= ckey(newest_tracked) or ch in tracked:
                continue
            phase, rtype = info["support_phase"], info["release_type"]

            if phase == "preview":
                notices.append(
                    f"{ch} is in preview ({rtype}, SDK {info['latest_sdk']}) — "
                    "not packageable: conda-build rejects the hyphen in the "
                    "version, and a preview would outrank stable anyway"
                )
                continue
            if phase != "active":
                notices.append(f"{ch} is {phase} ({rtype}) — not tracked, no action")
                continue
            if policy == "manual":
                notices.append(
                    f"{ch} is active ({rtype}) — policy is `manual`, so not escalated"
                )
                continue
            if policy == "lts" and rtype != "lts":
                notices.append(
                    f"{ch} went active as {rtype.upper()} (SDK "
                    f"{info['latest_sdk']}) — policy is `lts`, so no adoption "
                    "proposed. Change `policy` to `latest` if you want STS lines."
                )
                continue

            # This line reached GA and policy says adopt it. If the outgoing
            # line lives on `main`, the transition is mechanical: cut a vN branch
            # so it stays patchable, then move `main`. Emit it as a structured
            # transition rather than an issue -- it is actionable, and leaving it
            # as prose is how 9.0 ended up orphaned.
            out_ch = newest_tracked
            out_branch = tracked[out_ch]
            cut = "v" + out_ch.split(".")[0]
            problem = updater.conda_version_problem(info["latest_sdk"])
            if transition_cfg.get("enabled") and out_branch == "main" and not problem:
                if git_show(repo, cut, "recipe/meta.yaml") is not None:
                    notices.append(
                        f"{ch} is GA and would take over `main`, but branch "
                        f"`{cut}` already exists — not proposing a transition. "
                        f"Add `\"{out_ch}\": \"{cut}\"` to `tracked` if that "
                        "branch is the outgoing line."
                    )
                    continue
                transition = {
                    "to_channel": ch,
                    "to_sdk": info["latest_sdk"],
                    "to_release_type": rtype,
                    "from_channel": out_ch,
                    "from_branch": out_branch,
                    "cut_branch": cut,
                    "cut_branch_upstream": bool(
                        transition_cfg.get("cut_branch_upstream")
                    ),
                }
                continue
            issues.append(
                {
                    "key": f"new-channel-{ch}",
                    "title": (
                        f".NET {ch} is now active "
                        f"({info['release_type'].upper()}) -- decide whether to track it"
                    ),
                    "body": (
                        f"Microsoft lists **{ch}** as `active`, "
                        f"`{info['release_type']}`, latest SDK "
                        f"`{info['latest_sdk']}`.\n\n"
                        f"Newest tracked line is **{newest_tracked}** "
                        f"(branch `{tracked[newest_tracked]}`), with "
                        f"`policy: {policy}`.\n\n"
                        "The convention here is that the newest line lives on "
                        f"`main` and the previous one gets a `vN` branch. So "
                        f"adopting {ch} means: cut "
                        f"`v{newest_tracked.split('.')[0]}` from `main` first (so "
                        f"{newest_tracked} stays patchable), then move `main` to "
                        f"{ch} and update `tracked`.\n\n"
                        "No PR was opened -- this is a policy decision."
                    ),
                }
            )
    # If a transition is pending, drop the outgoing line's bump. Both would
    # target the same branch -- the bump moving `main` to 10.0.302 while the
    # transition moves it to 11.0.100 -- and the PRs would conflict. The
    # outgoing line's patches belong on its new vN branch, which the next run
    # picks up once `tracked` maps it there. Self-healing rather than ordered.
    if transition:
        dropped = [b for b in bumps if b["channel"] == transition["from_channel"]]
        if dropped:
            bumps = [b for b in bumps if b["channel"] != transition["from_channel"]]
            d = dropped[0]
            notices.append(
                f"Deferred the {d['channel']} bump ({d['current_sdk']} → "
                f"{d['latest_sdk']}): a transition to {transition['to_channel']} "
                f"is pending on the same branch. It will be proposed against "
                f"`{transition['cut_branch']}` once that branch exists and is "
                "tracked."
            )

    return bumps, issues, lines, notices, transition


def plan_rids(cfg, offered, packaged):
    """Audit the architectures Microsoft offers against the ones we package."""
    rid_map = cfg.get("rid_map", {})
    ignore = set(cfg.get("ignore_rids", []))
    active = set(cfg.get("active_subdirs", []))
    threshold = int(cfg.get("min_repodata_bytes", 0))
    issues, skipped = [], []

    for rid in sorted(packaged - set(offered)):
        issues.append(
            {
                "key": f"rid-dropped-{rid}",
                "title": f"Microsoft no longer publishes {rid} -- recipe still packages it",
                "body": (
                    f"`{rid}` is in the recipe's platform set but is **not** among "
                    "the SDK artifacts Microsoft publishes.\n\n"
                    "This is impending breakage, not an opportunity: the next "
                    "version bump fails outright when the artifact cannot be "
                    "found. Both the recipe's selectors and `PLATFORMS` need "
                    "updating, and the conda-forge platform should be dropped "
                    "from `conda-forge.yml`."
                ),
            }
        )

    for rid in sorted(set(offered) - packaged):
        subdir = rid_map.get(rid)
        if rid in ignore:
            skipped.append({"rid": rid, "reason": "no conda-forge equivalent"})
            continue
        if subdir is None:
            skipped.append({"rid": rid, "reason": "unmapped"})
            continue
        if subdir not in active:
            skipped.append(
                {"rid": rid, "reason": f"{subdir} is not actively built by conda-forge"}
            )
            continue
        size = repodata_size(subdir)
        if size is None:
            skipped.append({"rid": rid, "reason": f"no {subdir} subdir"})
            continue
        if size < threshold:
            skipped.append(
                {"rid": rid, "reason": f"{subdir} repodata only {size:,} B -- no ecosystem"}
            )
            continue
        issues.append(
            {
                "key": f"rid-available-{rid}",
                "title": f"Microsoft publishes {rid} -- candidate for conda-forge {subdir}",
                "body": (
                    f"Microsoft publishes an SDK archive for **{rid}**, mapping to "
                    f"conda-forge's `{subdir}`, which the recipe does not package.\n\n"
                    f"`{subdir}` looks viable: `repodata.json` is {size:,} bytes "
                    f"(threshold {threshold:,}), so there is a real package "
                    "ecosystem to depend on.\n\n"
                    "Adding it means:\n\n"
                    f"1. a `sha256` and `platform` selector pair for `{rid}`;\n"
                    "2. **narrowing** any existing broader selector so it stops "
                    "matching the new arch -- easy to miss, and silently ships "
                    "the wrong hash if you do;\n"
                    f"3. `build_platform` in `conda-forge.yml` if cross-compiling;\n"
                    f"4. adding `{rid}` to `PLATFORMS` in the updater;\n"
                    "5. a rerender, or the platform is declared but never built.\n\n"
                    "Tests may be skipped for a cross-compiled platform, so the "
                    "first real validation is a user installing it."
                ),
            }
        )
    return issues, skipped


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 1
    cfg = strip_comments(json.loads(Path(argv[1]).read_text()))
    repo = Path(argv[2])

    updater = load_updater()

    index = updater.fetch_json(updater.INDEX_URL)
    raw = index.get("releases-index", [])
    # Channel strings become shell arguments and branch names downstream, so
    # drop anything that is not a plain dotted number rather than passing it on.
    bad = [
        e.get("channel-version")
        for e in raw
        if not updater.CHANNEL_RE.match(str(e.get("channel-version", "")))
    ]
    if bad:
        print(f"warning: ignoring implausible channel ids {bad!r}", file=sys.stderr)
    raw = [e for e in raw if updater.CHANNEL_RE.match(str(e.get("channel-version", "")))]
    channels = [
        {
            "channel": e.get("channel-version"),
            "support_phase": e.get("support-phase"),
            "release_type": e.get("release-type"),
            "latest_sdk": e.get("latest-sdk"),
            "eol_date": e.get("eol-date"),
        }
        for e in raw
    ]

    bumps, issues, lines, notices, transition = plan_lines(cfg, channels, repo, updater)

    # Which RIDs are packaged is a property of the recipe, not of this config, so
    # read it from the newest tracked branch. Reading the recipe rather than a
    # constant is what lets branches carry different shapes -- and the audit then
    # reflects what the recipe actually does, which is the drift it exists to
    # detect.
    packaged: set[str] = set()
    if lines:
        newest_line = max(lines, key=lambda l: ckey(l["channel"]))
        meta = git_show(repo, newest_line["branch"], "recipe/meta.yaml")
        found = updater.discover_platforms(meta) if meta else None
        packaged = {rid for _s, rid, _e in (found or updater.PLATFORMS)}
    else:
        packaged = {rid for _s, rid, _e in updater.PLATFORMS}


    # Architectures are audited once, against the newest tracked line: the RID
    # set is a property of .NET, not of a patch release.
    offered: list[str] = []
    if lines:
        newest = newest_line
        entry = next(
            (e for e in raw if e.get("channel-version") == newest["channel"]), None
        )
        if entry:
            releases = updater.fetch_json(entry["releases.json"]).get("releases", [])
            if releases:
                offered = sorted(
                    {
                        f["rid"]
                        for f in releases[0]["sdk"].get("files", [])
                        if str(f.get("name", "")).endswith((".tar.gz", ".zip"))
                    }
                )

    rid_issues, rid_skipped = plan_rids(cfg, offered, packaged) if offered else ([], [])
    issues.extend(rid_issues)

    print(
        json.dumps(
            {
                "policy": cfg.get("policy"),
                "issue_repo": cfg.get("issue_repo"),
                "lines": lines,
                "notices": notices,
                "transition": transition,
                "bumps": bumps,
                "rids": {
                    "offered": offered,
                    "packaged": sorted(packaged),
                    "skipped": rid_skipped,
                },
                "issues": issues,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
