#!/usr/bin/env python3
"""Turn discovery output into a plan: what to bump, and what to escalate.

The organising principle: **mechanical changes become PRs, judgment calls become
issues.**

A patch bump inside the tracked channel is mechanical -- two version variables
and five hashes, all derivable from Microsoft's metadata. That gets a PR.

Everything else needs a human:

* A new release line appearing. A conda-forge feedstock publishes one `dotnet`,
  so adopting 11.0 means abandoning 10.0. Whether a scientific packaging channel
  should follow STS or stay on LTS is a policy question, not a data question.
* The tracked line reaching end of life. Historically handled with a final
  "update to end of life version" PR (dotnet-feedstock #99, #100, #103) before
  moving on.
* Microsoft starting to publish a new architecture. Might be worth packaging,
  might be a subdir with no ecosystem -- see the viability check below.
* Microsoft *dropping* an architecture the recipe packages. This one is
  impending breakage: the next bump would fail outright when the artifact
  can't be found.

Usage:
    plan.py channels.json path/to/dotnet-feedstock          > plan.json
    plan.py channels.json path/to/dotnet-feedstock --offline < cached.json

Exit codes: 0 always (a plan with nothing in it is a valid plan). Errors exit 1.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
REPODATA = "https://conda.anaconda.org/conda-forge/{subdir}/repodata.json"
USER_AGENT = "dotnet-feedstock-autobump"


def load_updater():
    """Import the updater so PLATFORMS is read, never duplicated.

    The set of RIDs the recipe packages lives in exactly one place. Copying it
    here would let the audit drift out of sync with what the recipe does, which
    is the specific failure this is meant to detect.
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


def repodata_size(subdir: str) -> int | None:
    """Content-Length of a subdir's repodata.json, via HEAD. None if absent.

    A cheap proxy for "does this subdir have an ecosystem". Actually counting
    packages would mean downloading linux-64's ~432 MB repodata, which is not
    viable in CI. Heuristic, not a guarantee.
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
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        return None
    except urllib.error.URLError:
        return None


def recipe_versions(recipe: Path) -> tuple[str, str]:
    text = recipe.read_text()
    sdk = re.search(r'set\s+sdk_version\s*=\s*"([^"]*)"', text)
    rt = re.search(r'set\s+runtime_version\s*=\s*"([^"]*)"', text)
    return (sdk.group(1) if sdk else "?", rt.group(1) if rt else "?")


def channel_sort_key(ch: str):
    return [int(x) if x.isdigit() else 0 for x in ch.split(".")]


def plan_channels(cfg: dict, channels: list[dict], cur_sdk: str) -> tuple[dict, list]:
    """Decide whether to bump, and whether any channel change needs escalating."""
    track = cfg["track"]
    policy = cfg.get("policy", "manual")
    issues: list[dict] = []

    tracked = next((c for c in channels if c["channel"] == track), None)
    if tracked is None:
        issues.append(
            {
                "key": f"tracked-channel-missing-{track}",
                "title": f"Tracked channel {track} is no longer in Microsoft's release index",
                "body": (
                    f"`channels.json` tracks **{track}**, but that channel is not "
                    "present in `releases-index.json` any more.\n\n"
                    "Nothing can be bumped until `track` is corrected."
                ),
            }
        )
        return {}, issues

    bump = {}
    if tracked["latest_sdk"] != cur_sdk:
        bump = {
            "channel": track,
            "current_sdk": cur_sdk,
            "latest_sdk": tracked["latest_sdk"],
        }

    # Tracked line going EOL is a decision, not a bump.
    if tracked["support_phase"] == "eol":
        issues.append(
            {
                "key": f"tracked-eol-{track}",
                "title": f"Tracked channel {track} has reached end of life",
                "body": (
                    f"`channels.json` tracks **{track}**, whose `support-phase` is "
                    f"now `eol` (EOL date: {tracked.get('eol_date') or 'unstated'}).\n\n"
                    f"The pattern in this feedstock's history is a final "
                    f"\"update to end of life version\" PR "
                    f"(conda-forge/dotnet-feedstock#99, #100, #103), then moving "
                    f"`track` forward.\n\n"
                    f"Latest {track} SDK is `{tracked['latest_sdk']}`; the recipe "
                    f"has `{cur_sdk}`."
                ),
            }
        )

    # Candidate newer lines, judged against the declared policy.
    if policy != "manual":
        for c in channels:
            if channel_sort_key(c["channel"]) <= channel_sort_key(track):
                continue
            if c["support_phase"] != "active":
                continue  # preview is not a candidate; we don't ship previews
            if policy == "lts" and c["release_type"] != "lts":
                # Still worth one line in the plan so it isn't invisible, but no
                # issue: the declared policy already answers this.
                continue
            issues.append(
                {
                    "key": f"new-channel-{c['channel']}",
                    "title": (
                        f".NET {c['channel']} is now active "
                        f"({c['release_type'].upper()}) -- decide whether to track it"
                    ),
                    "body": (
                        f"Microsoft's release index now lists **{c['channel']}** as "
                        f"`support-phase: active`, `release-type: "
                        f"`{c['release_type']}`, latest SDK `{c['latest_sdk']}`.\n\n"
                        f"This feedstock tracks **{track}** "
                        f"(`{tracked['release_type']}`), per `channels.json` with "
                        f"`policy: {policy}`.\n\n"
                        "A conda-forge feedstock publishes one `dotnet` package, so "
                        "adopting a new line means leaving the current one. Options:\n\n"
                        f"- Switch: set `track` to `{c['channel']}` and open a "
                        f"`v{c['channel'].split('.')[0]}update` branch.\n"
                        f"- Stay on {track} and revisit when it nears EOL.\n"
                        f"- Bump {track} to its final version first "
                        "(the #99/#100/#103 pattern), then switch.\n\n"
                        "No PR was opened -- this is a policy decision."
                    ),
                }
            )
    return bump, issues


def plan_rids(cfg: dict, offered: list[str], packaged: set[str]) -> list[dict]:
    """Audit the architectures Microsoft offers against the ones we package."""
    rid_map = cfg.get("rid_map", {})
    ignore = set(cfg.get("ignore_rids", []))
    active = set(cfg.get("active_subdirs", []))
    threshold = int(cfg.get("min_repodata_bytes", 0))
    issues: list[dict] = []

    # Dropped: packaged but no longer offered. This breaks the next bump.
    for rid in sorted(packaged - set(offered)):
        issues.append(
            {
                "key": f"rid-dropped-{rid}",
                "title": f"Microsoft no longer publishes {rid} -- recipe still packages it",
                "body": (
                    f"`{rid}` is in the recipe's platform set but is **not** in the "
                    "SDK artifacts Microsoft publishes for the tracked channel.\n\n"
                    "This is impending breakage, not an opportunity: the next "
                    "version bump will fail outright when the artifact cannot be "
                    "found. The recipe's selectors and `PLATFORMS` both need "
                    "updating, and the corresponding conda-forge platform should "
                    "be dropped from `conda-forge.yml`."
                ),
            }
        )

    # Added: offered, mappable, viable, but not packaged.
    for rid in sorted(set(offered) - packaged - ignore):
        subdir = rid_map.get(rid)
        if subdir is None:
            continue  # unmappable and not explicitly ignored -- stay quiet
        if subdir not in active:
            continue  # frozen subdir, e.g. win-32 (last built 2019)
        size = repodata_size(subdir)
        if size is None:
            continue  # conda-forge has no such subdir at all
        if size < threshold:
            continue  # subdir exists but has no ecosystem (the armv7l case)
        issues.append(
            {
                "key": f"rid-available-{rid}",
                "title": f"Microsoft publishes {rid} -- candidate for conda-forge {subdir}",
                "body": (
                    f"Microsoft publishes an SDK archive for **{rid}**, which maps "
                    f"to conda-forge's `{subdir}`. The recipe does not package it.\n\n"
                    f"`{subdir}` looks viable: its `repodata.json` is "
                    f"{size:,} bytes (threshold {threshold:,}), so it has a real "
                    "package ecosystem to depend on.\n\n"
                    "Adding it means:\n\n"
                    f"1. a `sha256` and `platform` selector pair for `{rid}`;\n"
                    "2. **narrowing** any existing broader selector so it stops "
                    "matching the new arch -- this bit is easy to get wrong and "
                    "silently ships the wrong hash;\n"
                    f"3. `build_platform: {{{subdir.replace('-', '_')}: ...}}` in "
                    "`conda-forge.yml` if cross-compiling;\n"
                    f"4. adding `{rid}` to `PLATFORMS` in the updater;\n"
                    "5. a rerender, or the platform is declared but never built.\n\n"
                    "Note tests may be skipped for a cross-compiled platform, so "
                    "the first real validation is a user installing it."
                ),
            }
        )

    # Offered but non-viable / unmappable: reported in the plan, no issue raised.
    skipped = []
    for rid in sorted(set(offered) - packaged):
        if rid in ignore:
            skipped.append({"rid": rid, "reason": "no conda-forge equivalent"})
            continue
        subdir = rid_map.get(rid)
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
        elif size < threshold:
            skipped.append(
                {"rid": rid, "reason": f"{subdir} repodata only {size:,} B -- no ecosystem"}
            )
    return issues, skipped


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 1
    cfg = strip_comments(json.loads(Path(argv[1]).read_text()))
    feedstock = Path(argv[2])
    recipe = feedstock / "recipe" / "meta.yaml"
    if not recipe.exists():
        print(f"error: {recipe} not found", file=sys.stderr)
        return 1

    updater = load_updater()
    packaged = {rid for _sel, rid, _ext in updater.PLATFORMS}

    index = updater.fetch_json(updater.INDEX_URL)
    channels = [
        {
            "channel": e.get("channel-version"),
            "support_phase": e.get("support-phase"),
            "release_type": e.get("release-type"),
            "latest_sdk": e.get("latest-sdk"),
            "eol_date": e.get("eol-date"),
        }
        for e in index.get("releases-index", [])
    ]

    cur_sdk, cur_rt = recipe_versions(recipe)
    bump, issues = plan_channels(cfg, channels, cur_sdk)

    offered: list[str] = []
    tracked = next((c for c in channels if c["channel"] == cfg["track"]), None)
    if tracked:
        entry = next(
            e
            for e in index["releases-index"]
            if e.get("channel-version") == cfg["track"]
        )
        releases = updater.fetch_json(entry["releases.json"]).get("releases", [])
        if releases:
            sdk = releases[0]["sdk"]
            offered = sorted(
                {
                    f["rid"]
                    for f in sdk.get("files", [])
                    if str(f.get("name", "")).endswith((".tar.gz", ".zip"))
                }
            )

    rid_issues, rid_skipped = plan_rids(cfg, offered, packaged) if offered else ([], [])
    issues.extend(rid_issues)

    print(
        json.dumps(
            {
                "track": cfg["track"],
                "policy": cfg.get("policy"),
                "issue_repo": cfg.get("issue_repo"),
                "recipe": {"sdk_version": cur_sdk, "runtime_version": cur_rt},
                "channels": channels,
                "rids": {
                    "offered": offered,
                    "packaged": sorted(packaged),
                    "skipped": rid_skipped,
                },
                "bump": bump or None,
                "issues": issues,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
