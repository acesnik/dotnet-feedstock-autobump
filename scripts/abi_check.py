#!/usr/bin/env python3
"""Compare the ABI the artifact needs against the ABI the recipe declares.

    abi_check.py channels.json path/to/feedstock plan.json > merged-plan.json

Reads the plan, probes each tracked line's Linux artifacts (see abi_probe.py),
and merges any findings back into the plan's `issues` and `notices`. Merging
rather than emitting separately is deliberate: the workflow's issue-filing and
run-summary steps already consume `plan.json`, so ABI findings escalate through
exactly the same path -- including the dedup-by-key that stops an issue being
re-filed weekly -- with no second mechanism to keep in step.

Why this exists at all: the bot tracked two version-coupled facts (`sdk_version`
and `runtime_version`) and verified one integrity fact (sha256 against
Microsoft's sha512). The ABI floor is a **third** version-coupled fact, and it
had already drifted unnoticed -- .NET 10 raised its glibc requirement from 2.17
to 2.27 while the recipe went on declaring 2.17, so the published package
advertised a floor it did not meet. A bump touches versions and hashes, so
nothing here could have caught it.

Escalation follows the repo's rule -- mechanical to a PR, judgment to an issue.
Nothing here is mechanical: raising `c_stdlib_version` drops users, and changing
an `openssl` pin changes what resolves for everyone. So this file never edits a
recipe. It escalates, exactly like the existing "Microsoft dropped an
architecture" check.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import urllib.error
from pathlib import Path

HERE = Path(__file__).parent

# conda-forge's global pinning today. Used ONLY when the recipe declares nothing
# and no rendered .ci_support is available -- and it emits a notice saying so,
# because assuming a floor is how the drift being checked for happened.
DEFAULT_GLIBC = "2.17"

# RID -> the .ci_support basename prefix conda-smithy renders for it.
CI_SUPPORT_PREFIX = {"linux-x64": "linux_64", "linux-arm64": "linux_aarch64"}

OPENSSL_LINE_RE = re.compile(
    r"^\s*-\s*openssl\b([^#\n]*?)\s*(?:#\s*\[([^\]]+)\])?\s*$", re.M
)
CBC_ENTRY_RE = re.compile(r'^\s*-\s*"?([^"#\s]+)"?\s*(?:#\s*\[([^\]]+)\])?\s*$', re.M)
CONSTRAINT_RE = re.compile(r"^(<=|>=|==|!=|<|>|=)?\s*([0-9][0-9a-zA-Z.*_]*)$")


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def selector_ns(rid: str) -> dict:
    """conda-build's selector namespace for a Linux RID.

    `arm64` is False for linux-arm64 on purpose: conda-build derives it from the
    architecture string, which is `aarch64` on Linux and `arm64` only on macOS.
    Every arm64 selector in this recipe is paired with `osx` or `win`, so the
    distinction cannot silently mis-resolve one -- but getting it backwards would
    make `# [aarch64]` and `# [osx and arm64]` interchangeable, which they are
    not.
    """
    aarch64 = rid == "linux-arm64"
    return {
        "linux": True,
        "unix": True,
        "osx": False,
        "win": False,
        "aarch64": aarch64,
        "arm64": False,
        "x86_64": rid == "linux-x64",
        "x86": False,
        "ppc64le": False,
        "s390x": False,
        "riscv64": False,
        "build_platform": rid,
        "target_platform": rid,
    }


def selector_true(expr: str | None, ns: dict) -> bool | None:
    """Evaluate a selector. None means "could not tell" -- never a silent False.

    A selector this cannot evaluate must not be treated as inapplicable: that
    would read a pin as absent and turn a real constraint into a phantom finding.
    """
    if expr is None:
        return True
    try:
        return bool(eval(expr, {"__builtins__": {}}, dict(ns)))  # noqa: S307
    except Exception:
        return None


def declared_glibc(cbc_text: str | None, rid: str, problems: list | None = None):
    """`c_stdlib_version` for this RID from recipe/conda_build_config.yaml.

    A selector that cannot be evaluated is appended to `problems` rather than
    quietly skipped. Skipping it would report "nothing declared" and fall back to
    the rendered value, which is the same silent-wrong-number failure this whole
    module exists to catch.
    """
    if not cbc_text:
        return None
    block = re.search(r"^c_stdlib_version:\s*\n((?:\s+.*\n?)*)", cbc_text, re.M)
    if not block:
        return None
    ns = selector_ns(rid)
    for value, sel in CBC_ENTRY_RE.findall(block.group(1)):
        applies = selector_true(sel or None, ns)
        if applies is None:
            if problems is not None:
                problems.append(sel)
            continue
        if applies:
            return value
    return None


def rendered_glibc(ci_text: str | None) -> str | None:
    """`c_stdlib_version` as conda-smithy actually rendered it."""
    if not ci_text:
        return None
    m = re.search(r"^c_stdlib_version:\s*\n\s*-\s*'?\"?([0-9.]+)", ci_text, re.M)
    return m.group(1) if m else None


def permits(constraint: str, version: str, vkey) -> bool | None:
    """Does this conda version constraint admit `version`?

    Handles the comma-separated comparison forms a recipe actually uses. Returns
    None on anything it does not recognise, so an unparseable pin surfaces as
    "check this by hand" instead of a confident wrong verdict.
    """
    text = (constraint or "").strip()
    if not text:
        return True  # no constraint: everything is admitted
    for clause in (c.strip() for c in text.split(",")):
        if not clause:
            continue
        m = CONSTRAINT_RE.match(clause)
        if not m:
            return None
        op, bound = m.group(1) or "==", m.group(2)
        if "*" in bound:
            if op not in ("==", "="):
                return None
            prefix = bound.rstrip("*").rstrip(".")
            if not (version == prefix or version.startswith(prefix + ".")):
                return False
            continue
        a, b = vkey(version), vkey(bound)
        # `==`/`=` on a shorter bound is a prefix match in conda ("=3" admits
        # 3.1), so compare only as many components as the bound specifies.
        if op in ("==", "="):
            if a[: len(b)] != b:
                return False
        elif op == "!=":
            if a[: len(b)] == b:
                return False
        elif op == "<":
            if not a < b:
                return False
        elif op == "<=":
            if not a <= b:
                return False
        elif op == ">":
            if not a > b:
                return False
        elif op == ">=":
            if not a >= b:
                return False
        else:  # pragma: no cover - CONSTRAINT_RE admits nothing else
            return None
    return True


def declared_openssl(meta_text: str | None, rid: str):
    """(constraint, selector) for every openssl run dep applying to this RID.

    A list, not a single value: an output could legitimately carry more than one
    openssl line behind different selectors, and silently taking the first would
    read the wrong one.
    """
    if not meta_text:
        return []
    ns = selector_ns(rid)
    out = []
    for constraint, sel in OPENSSL_LINE_RE.findall(meta_text):
        applies = selector_true(sel or None, ns)
        if applies is None:
            out.append((constraint.strip(), sel, None))
        elif applies:
            out.append((constraint.strip(), sel or None, True))
    return out


def openssl_verdict(declared, supported: list[str], known: dict, vkey):
    """Which openssl majors the pin admits but the runtime cannot load.

    The undeclared case needs no special handling and gets none: no openssl line
    means no constraint, which admits every major, so an unsupported one shows up
    as admitted-but-unloadable by the same arithmetic. That is the honest reading
    -- nothing stops openssl 4 being present in the environment.
    """
    if not declared:
        constraints = [""]
    else:
        constraints = [c for c, _sel, ok in declared if ok is not False]

    unparseable = []
    admitted: set[str] = set()
    for constraint in constraints:
        for maj in known:
            verdict = permits(constraint, maj, vkey)
            if verdict is None:
                unparseable.append(constraint)
            elif verdict:
                admitted.add(maj)
    sup = set(supported)
    return {
        "declared": [
            {"constraint": c, "selector": s} for c, s, ok in declared if ok is not False
        ],
        # A selector we could not evaluate is included in the comparison above
        # (erring toward "it applies"), but reported so the guess is visible.
        "unevaluable_selectors": [s for _c, s, ok in declared if ok is None],
        "undeclared": not declared,
        "admitted": sorted(admitted, key=vkey),
        "supported": sorted(sup, key=vkey),
        "admitted_unsupported": sorted(admitted - sup, key=vkey),
        "supported_excluded": sorted(sup - admitted, key=vkey),
        "unparseable": sorted(set(unparseable)),
    }


def probe_line(line: dict, rids, repo: Path, plan, probe_fn) -> dict:
    """Probe one release line: declared vs required, per Linux RID."""
    branch = line["branch"]
    meta = plan.git_show(repo, branch, "recipe/meta.yaml")
    cbc = plan.git_show(repo, branch, "recipe/conda_build_config.yaml")
    # Probe the version that will SHIP: the bump target if the line is stale,
    # otherwise what it already carries. One download either way.
    runtime = line.get("latest_runtime") or line.get("current_runtime")
    out = {
        "channel": line["channel"],
        "branch": branch,
        "runtime_version": runtime,
        "rids": {},
    }
    for rid in rids:
        entry: dict = {}
        prefix = CI_SUPPORT_PREFIX.get(rid)
        ci = (
            plan.git_show(repo, branch, f".ci_support/{prefix}_.yaml")
            if prefix
            else None
        )
        problems: list = []
        entry["glibc_declared"] = declared_glibc(cbc, rid, problems)
        entry["glibc_selector_problems"] = problems
        entry["glibc_rendered"] = rendered_glibc(ci)
        try:
            entry["probe"] = probe_fn(rid, runtime)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as e:
            entry["probe"] = None
            entry["probe_error"] = f"{type(e).__name__}: {e}"
        entry["openssl_declared"] = declared_openssl(meta, rid)
        out["rids"][rid] = entry
    return out


def findings(result: dict, known: dict, vkey) -> tuple[list, list]:
    """Turn one line's probe into (issues, notices)."""
    issues, notices = [], []
    ch, branch = result["channel"], result["branch"]
    runtime = result["runtime_version"]

    glibc_bad, ssl_bad = [], []

    for rid, e in sorted(result["rids"].items()):
        if e.get("probe") is None:
            notices.append(
                f"could not probe {ch} `{rid}` (runtime {runtime}): "
                f"{e.get('probe_error', 'unknown error')} — ABI unverified for "
                "this platform, so treat a clean report as incomplete"
            )
            continue
        probe = e["probe"]
        floor = probe.get("glibc_floor")
        for sel in e.get("glibc_selector_problems") or []:
            notices.append(
                f"{ch} `{rid}`: could not evaluate the selector `{sel}` on a "
                "`c_stdlib_version` entry, so that entry was ignored — the "
                "declared floor below may be wrong"
            )
        intent, rendered = e.get("glibc_declared"), e.get("glibc_rendered")
        effective = intent or rendered or DEFAULT_GLIBC
        if intent is None and rendered is None:
            notices.append(
                f"{ch} `{rid}` declares no `c_stdlib_version` and has no rendered "
                f"`.ci_support`, so {DEFAULT_GLIBC} was assumed from conda-forge's "
                "global pinning — verify it"
            )
        if intent and rendered and vkey(intent) != vkey(rendered):
            notices.append(
                f"{ch} `{rid}`: recipe declares `c_stdlib_version {intent}` but "
                f"`.ci_support` still renders {rendered} — needs a rerender before "
                "it takes effect"
            )
        if floor is None:
            notices.append(
                f"{ch} `{rid}`: no GLIBC_ version requirements found in "
                f"{probe.get('glibc_inspected', 0)} inspected objects — unexpected "
                "for a Linux artifact, so the parse is suspect"
            )
        else:
            if not vkey(floor) <= vkey(effective):
                glibc_bad.append((rid, floor, effective, probe.get("glibc_driver")))

        v = openssl_verdict(
            e.get("openssl_declared") or [], probe.get("openssl_majors") or [], known, vkey
        )
        e["openssl_verdict"] = v
        for sel in v["unevaluable_selectors"]:
            notices.append(
                f"{ch} `{rid}`: could not evaluate the selector `{sel}` on an "
                "openssl dependency; assumed it applies, which is the cautious "
                "direction but may be wrong"
            )
        if v["unparseable"]:
            notices.append(
                f"{ch} `{rid}`: could not parse openssl constraint(s) "
                + ", ".join(f"`{c}`" for c in v["unparseable"])
                + " — check by hand"
            )
        if v["admitted_unsupported"]:
            ssl_bad.append((rid, v))
        elif v["supported_excluded"]:
            notices.append(
                f"{ch} `{rid}`: openssl pin excludes "
                + ", ".join(f"`{m}`" for m in v["supported_excluded"])
                + f", which runtime {runtime} can actually load "
                f"(`{'`, `'.join(probe['openssl_sonames'])}`) — the pin is now "
                "stricter than necessary"
            )

    if glibc_bad:
        rows = "\n".join(
            f"| `{rid}` | **{floor}** | {eff} | {', '.join((drv or [])[:2]) or '—'} |"
            for rid, floor, eff, drv in glibc_bad
        )
        issues.append(
            {
                "key": f"glibc-floor-{ch}-{runtime}",
                "title": (
                    f".NET {ch} requires a newer glibc than the recipe declares "
                    f"(runtime {runtime})"
                ),
                "body": (
                    f"Microsoft's prebuilt runtime **{runtime}** requires a higher "
                    f"glibc than `{branch}` declares via `c_stdlib_version`. The "
                    "published packages therefore advertise a floor they do not "
                    "meet: conda installs them on hosts where the runtime cannot "
                    "load, so the failure lands at first use rather than at solve "
                    "time.\n\n"
                    "| platform | runtime requires | recipe declares | driven by |\n"
                    "|---|---|---|---|\n" + rows + "\n\n"
                    "Measured from the runtime tarball's `.gnu.version_r` sections, "
                    "which is the same information `check-glibc` from "
                    "`cf-nvidia-tools` reports.\n\n"
                    "**No PR was opened.** Raising `c_stdlib_version` drops every "
                    "user below the new floor, which is a maintainer's call, not a "
                    "mechanical edit. Note also that conda-forge ships only certain "
                    "sysroots (2.12, 2.17, 2.28, 2.34, 2.39), so the fix is usually "
                    "the next one at or above the measured floor, which "
                    "over-restricts slightly.\n\n"
                    "This is not hypothetical: .NET 10 raised its floor from 2.17 to "
                    "2.27 and the recipe went on declaring 2.17 for the whole line."
                ),
            }
        )

    if ssl_bad:
        def _pin(v):
            if v["undeclared"]:
                return "*(undeclared)*"
            return ", ".join(
                f"`openssl {d['constraint']}`" if d["constraint"] else "`openssl`"
                for d in v["declared"]
            )

        rows = "\n".join(
            f"| `{rid}` | {_pin(v)} | {', '.join(v['admitted'])} | "
            f"{', '.join(v['supported'])} | "
            f"**{', '.join(v['admitted_unsupported'])}** |"
            for rid, v in ssl_bad
        )
        issues.append(
            {
                "key": f"openssl-soname-{ch}-{runtime}",
                "title": (
                    f".NET {ch} can be resolved against an openssl it cannot load "
                    f"(runtime {runtime})"
                ),
                "body": (
                    f".NET's crypto shim `dlopen`s a hardcoded list of sonames. For "
                    f"runtime **{runtime}** that list does not include every openssl "
                    f"the solver may pick for `{branch}`.\n\n"
                    "| platform | recipe pin | admits | runtime can load | unloadable |\n"
                    "|---|---|---|---|---|\n" + rows + "\n\n"
                    "When an unloadable openssl is selected, every crypto call fails "
                    'with "No usable version of libssl was found" — the package '
                    "installs and resolves cleanly, so this does not show up as a "
                    "solver error.\n\n"
                    "*(undeclared)* means no `openssl` run dependency applies to that "
                    "platform, which is not protection: nothing prevents another "
                    "package pulling a newer openssl into the environment.\n\n"
                    "**No PR was opened** — changing an openssl pin changes what "
                    "resolves for every user of the package."
                ),
            }
        )

    return issues, notices


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print(__doc__, file=sys.stderr)
        return 2
    plan = load("plan", "plan.py")
    probe_mod = load("abi_probe", "abi_probe.py")

    cfg = plan.strip_comments(json.loads(Path(argv[1]).read_text()))
    repo = Path(argv[2])
    doc = json.loads(Path(argv[3]).read_text())

    abi_cfg = cfg.get("abi", {}) or {}
    if not abi_cfg.get("enabled", True):
        doc["abi"] = {"enabled": False}
        print(json.dumps(doc, indent=2))
        return 0

    updater = plan.load_updater()

    targets = list(doc.get("lines", []))
    # A pending transition moves `main` to a line that is not tracked yet, so its
    # ABI has never been checked against this recipe. Checking it here is the only
    # chance before that PR is opened -- and it is exactly where a floor moves,
    # since both drifts observed so far happened at a major boundary.
    tr = doc.get("transition")
    if tr:
        targets.append(
            {
                "channel": tr["to_channel"],
                "branch": tr["from_branch"],
                "latest_runtime": tr.get("to_runtime"),
                "current_runtime": tr.get("to_runtime"),
            }
        )

    results, issues, notices = [], [], []
    for line in targets:
        if not (line.get("latest_runtime") or line.get("current_runtime")):
            notices.append(
                f"no runtime version known for {line.get('channel')} — ABI not checked"
            )
            continue
        meta = plan.git_show(repo, line["branch"], "recipe/meta.yaml")
        found = updater.discover_platforms(meta) if meta else None
        rids = [
            rid
            for _sel, rid, _ext in (found or updater.PLATFORMS)
            if rid.startswith("linux-")
        ]
        # glibc and openssl sonames are ELF concepts; osx and win platforms have
        # neither, so probing them would download 60 MB to learn nothing.
        rids = [r for r in rids if r in CI_SUPPORT_PREFIX]
        if not rids:
            notices.append(
                f"{line['channel']} packages no Linux platform this can inspect — "
                "ABI not checked"
            )
            continue
        result = probe_line(line, rids, repo, plan, probe_mod.probe)
        results.append(result)
        line_issues, line_notices = findings(
            result, probe_mod.OPENSSL_SONAMES, probe_mod.vkey
        )
        issues.extend(line_issues)
        notices.extend(line_notices)

    doc.setdefault("issues", []).extend(issues)
    doc.setdefault("notices", []).extend(notices)
    doc["abi"] = {"enabled": True, "results": results}
    print(json.dumps(doc, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
