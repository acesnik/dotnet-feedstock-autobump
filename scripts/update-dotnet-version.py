#!/usr/bin/env python3
"""Update recipe/meta.yaml from Microsoft's published .NET release metadata.

Why this exists
---------------
This recipe can't be bumped by conda-forge's autotick bot (see issue #55), for
two structural reasons:

1. It carries *two* independent versions. `sdk_version` and `runtime_version`
   are not the same number and drift apart within a release line (10.0.302 vs
   10.0.10, say). The bot has no concept of a second version.
2. It carries one `sha256` per platform behind mutually-exclusive selectors. The
   bot updates `sha256:` under `source:`; it has no path into
   `{% set sha256 = "..." %}  # [linux and aarch64]`. How many there are, and
   what the selectors look like, varies per branch -- see discover_platforms.

Microsoft does publish everything needed as machine-readable JSON, including the
sdk/runtime pairing that defeats the bot:

    https://builds.dotnet.microsoft.com/dotnet/release-metadata/releases-index.json
      -> .../release-metadata/<channel>/releases.json

One wrinkle: that metadata publishes SHA-512, and conda-build accepts only
md5/sha1/sha256. So the hashes can't be lifted directly -- each artifact has to
be downloaded and re-hashed (this is issue #90). This script streams them
without writing to disk, and verifies the published SHA-512 as it goes, so a
truncated or substituted download can't silently produce a valid-looking
sha256.

Where this lives
----------------
Outside the feedstock, on purpose. A conda-forge feedstock's `.gitignore` reads
`*` / `!/conda-forge.yml` / `!/recipe/**` / `!/.ci_support/**` -- every root file
is ignored, and it says "Everything else is managed by the conda-smithy rerender
process. Please do not modify". So a helper script cannot be hosted at a
feedstock root. Run it from a feedstock checkout instead; the artifact it
produces is just the `meta.yaml` diff, which is all a PR needs.

Usage
-----
    cd path/to/dotnet-feedstock

    # show what the latest 10.0 release would look like (no downloads)
    /path/to/update-dotnet-version.py --channel 10.0 --dry-run

    # compute hashes and print the meta.yaml block
    ./update-dotnet-version.py --channel 10.0

    # ...and rewrite recipe/meta.yaml in place
    ./update-dotnet-version.py --channel 10.0 --write

    # pin an exact SDK rather than the channel's latest
    ./update-dotnet-version.py --sdk-version 10.0.302 --write

    # verify the checked-in recipe still matches upstream (exit 1 if not)
    ./update-dotnet-version.py --channel 10.0 --check

Standard library only, deliberately -- it should run in a bare CI container with
no pip install step.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

INDEX_URL = (
    "https://builds.dotnet.microsoft.com/dotnet/release-metadata/releases-index.json"
)

CHANNEL_RE = re.compile(r"^\d+\.\d+$")
SDK_RE = re.compile(r"^[0-9A-Za-z.+]+$")

# Fallback only. The real platform list is DISCOVERED from the target recipe by
# discover_platforms() -- see the note there. This is used when no recipe is
# available (e.g. --dry-run outside a feedstock) and describes the current shape
# on main.
PLATFORMS = [
    ("linux and aarch64", "linux-arm64", ".tar.gz"),
    ("linux and x86_64", "linux-x64", ".tar.gz"),
    ("osx and arm64", "osx-arm64", ".tar.gz"),
    ("osx and x86_64", "osx-x64", ".tar.gz"),
    # `win and x86_64` rather than a bare `win`: once win-arm64 is built, a bare
    # `# [win]` would hand the x64 hash to the arm64 build.
    ("win and x86_64", "win-x64", ".zip"),
    ("win and arm64", "win-arm64", ".zip"),
]
# Deliberately absent from the default: linux-arm (conda-forge's linux-armv7l).
# Microsoft ships the RID, but conda-forge's linux-armv7l subdir has 3 packages
# in total -- no icu, openssl or zlib for the runtime to depend on -- so a dotnet
# built there would be unusable. linux-musl-* are likewise unmapped: conda-forge
# has no musl subdir.

PLATFORM_LINE_RE = re.compile(
    r'^\{%\s*set\s+platform\s*=\s*"([^"]+)"\s*%\}\s*#\s*\[([^\]]+)\]\s*$', re.M
)


def discover_platforms(text: str) -> list[tuple[str, str, str]] | None:
    """Read the RID-to-selector mapping out of the recipe itself.

    A hardcoded list cannot work here. This tool bumps several branches, and each
    carries whatever recipe shape it was cut with: `main` has six platforms with
    `# [win and x86_64]`, while a branch from before win-arm64 has five with a
    bare `# [win]`. A single global list matches one of those and hard-fails on
    the other -- which it did, on both v8 and v9.

    The recipe already states the mapping unambiguously:

        {% set platform = "linux-arm64" %}  # [linux and aarch64]
        {% set platform = "win-x64" %}      # [win]

    so read it from there and the tool adapts to whatever it is pointed at.

    Extension is derived from the RID rather than parsed: the recipe's own `ext`
    selectors say `zip` for win and `tar.gz` otherwise, and Microsoft publishes
    exactly that.

    Returns None when no platform lines are present, so callers can fall back.
    """
    found = [
        (selector.strip(), rid, ".zip" if rid.startswith("win") else ".tar.gz")
        for rid, selector in (
            (m.group(1), m.group(2)) for m in PLATFORM_LINE_RE.finditer(text)
        )
    ]
    return found or None


def platforms_for(recipe: Path) -> list[tuple[str, str, str]]:
    """Platforms for a specific recipe, falling back to PLATFORMS if unreadable."""
    try:
        found = discover_platforms(recipe.read_text())
    except OSError:
        found = None
    if found is None:
        log(f"  note: no platform lines in {recipe}; using built-in default shape")
        return PLATFORMS
    return found

CHUNK = 1 << 20  # 1 MiB
USER_AGENT = "dotnet-feedstock-version-updater (+https://github.com/conda-forge/dotnet-feedstock)"


def log(msg: str) -> None:
    """Progress goes to stderr so stdout stays a clean, pipeable block."""
    print(msg, file=sys.stderr)


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.URLError as exc:
        sys.exit(f"error: could not fetch {url}: {exc}")


def find_channel(index: dict, channel: str) -> dict:
    for entry in index.get("releases-index", []):
        if entry.get("channel-version") == channel:
            return entry
    available = ", ".join(
        e.get("channel-version", "?") for e in index.get("releases-index", [])
    )
    sys.exit(f"error: channel {channel!r} not found. Available: {available}")


def pick_release(releases: list[dict], sdk_version: str | None) -> dict:
    """Latest release, or the one shipping an exact SDK version.

    A release's `sdk` is the primary SDK, but a release can carry several SDK
    feature bands in `sdks`; check both so `--sdk-version 10.0.203` resolves
    even when it isn't the primary.
    """
    if sdk_version is None:
        if not releases:
            sys.exit("error: channel has no releases")
        return releases[0]
    for rel in releases:
        if rel.get("sdk", {}).get("version") == sdk_version:
            return rel
        for sdk in rel.get("sdks", []):
            if sdk.get("version") == sdk_version:
                return rel
    sys.exit(f"error: no release found shipping SDK {sdk_version}")


def select_sdk(release: dict, sdk_version: str | None) -> dict:
    if sdk_version is None:
        return release["sdk"]
    if release.get("sdk", {}).get("version") == sdk_version:
        return release["sdk"]
    for sdk in release.get("sdks", []):
        if sdk.get("version") == sdk_version:
            return sdk
    return release["sdk"]


def find_artifact(sdk: dict, rid: str, ext: str) -> dict:
    """Locate one artifact by rid + extension.

    Filtering on extension matters: a single rid publishes several artifacts
    (osx-x64 has both .tar.gz and .pkg; win-x64 has both .zip and .exe), and
    the recipe wants the archive, not the installer.
    """
    matches = [
        f
        for f in sdk.get("files", [])
        if f.get("rid") == rid and str(f.get("name", "")).endswith(ext)
    ]
    if not matches:
        rids = sorted({f.get("rid", "?") for f in sdk.get("files", [])})
        sys.exit(
            f"error: no {ext} artifact for rid {rid!r} in SDK {sdk.get('version')}. "
            f"Published rids: {', '.join(rids)}"
        )
    if len(matches) > 1:
        names = ", ".join(m.get("name", "?") for m in matches)
        sys.exit(f"error: ambiguous artifact for {rid}{ext}: {names}")
    return matches[0]


def stream_hashes(url: str, expected_sha512: str | None) -> str:
    """Download once, return sha256, and verify the published sha512.

    Nothing is written to disk. The sha512 check is what makes the resulting
    sha256 trustworthy: it proves the bytes hashed are the bytes Microsoft
    published, so a truncated or MITM'd download fails loudly rather than
    yielding a plausible-looking hash.
    """
    sha256, sha512 = hashlib.sha256(), hashlib.sha512()
    total = 0
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            declared = int(resp.headers.get("Content-Length") or 0)
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                sha256.update(chunk)
                sha512.update(chunk)
                total += len(chunk)
    except urllib.error.URLError as exc:
        sys.exit(f"error: download failed for {url}: {exc}")

    if declared and total != declared:
        sys.exit(
            f"error: short read for {url}: got {total} bytes, expected {declared}"
        )

    got512 = sha512.hexdigest()
    if expected_sha512:
        if got512.lower() != expected_sha512.strip().lower():
            sys.exit(
                f"error: SHA-512 mismatch for {url}\n"
                f"  published: {expected_sha512}\n"
                f"  computed:  {got512}\n"
                "Refusing to emit a sha256 for bytes that don't match the "
                "published hash."
            )
    else:
        log("    warning: metadata published no hash; sha256 is unverified")

    return sha256.hexdigest()


def render_block(
    sdk_version: str,
    runtime_version: str,
    hashes: dict[str, str],
    platforms: list[tuple[str, str, str]],
) -> str:
    lines = [
        f'{{% set sdk_version = "{sdk_version}" %}}',
        f'{{% set runtime_version = "{runtime_version}" %}}',
        "{% set framework = '.'.join(sdk_version.split('.')[:2]) %}",
    ]
    width = max(len(f'{{% set sha256 = "{hashes[s]}" %}}') for s, _, _ in platforms)
    for selector, _rid, _ext in platforms:
        stmt = f'{{% set sha256 = "{hashes[selector]}" %}}'
        lines.append(f"{stmt.ljust(width)}  # [{selector}]")
    return "\n".join(lines)


def rewrite_meta(
    path: Path,
    sdk_version: str,
    runtime_version: str,
    hashes: dict[str, str],
    reset_build_number: bool,
) -> list[str]:
    """Surgically replace the version vars and per-selector sha256 lines.

    Deliberately line-oriented rather than a YAML round-trip: meta.yaml is
    Jinja-templated with selector comments, which no YAML library preserves.
    """
    text = path.read_text()
    original = text
    changes: list[str] = []
    # Discover from the file being edited, not from a global: this is the whole
    # point -- an older branch has a different set of selectors.
    platforms = discover_platforms(text) or PLATFORMS

    def set_var(src: str, name: str, value: str) -> str:
        pattern = re.compile(
            r'(\{%\s*set\s+' + re.escape(name) + r'\s*=\s*")([^"]*)("\s*%\})'
        )
        m = pattern.search(src)
        if not m:
            sys.exit(f"error: could not find `{{% set {name} = ... %}}` in {path}")
        if m.group(2) != value:
            changes.append(f"{name}: {m.group(2)} -> {value}")
        return pattern.sub(lambda mm: mm.group(1) + value + mm.group(3), src, count=1)

    text = set_var(text, "sdk_version", sdk_version)
    text = set_var(text, "runtime_version", runtime_version)

    for selector, _rid, _ext in platforms:
        # Anchor on the trailing selector comment -- that is the only thing
        # distinguishing the otherwise-identical sha256 lines.
        pattern = re.compile(
            r'(\{%\s*set\s+sha256\s*=\s*")([0-9a-fA-F]{64})("\s*%\}\s*#\s*\['
            + re.escape(selector)
            + r'\])'
        )
        m = pattern.search(text)
        if not m:
            sys.exit(
                f"error: could not find the sha256 line for selector "
                f"`# [{selector}]` in {path}. Has the recipe been restructured?"
            )
        new = hashes[selector]
        if m.group(2).lower() != new.lower():
            changes.append(f"sha256 [{selector}]: {m.group(2)[:12]}... -> {new[:12]}...")
        text = pattern.sub(lambda mm: mm.group(1) + new + mm.group(3), text, count=1)

    if reset_build_number and changes:
        bn = re.compile(r"(^\s*number:\s*)(\d+)(\s*$)", re.M)
        m = bn.search(text)
        if m and m.group(2) != "0":
            changes.append(f"build number: {m.group(2)} -> 0")
            text = bn.sub(lambda mm: mm.group(1) + "0" + mm.group(3), text, count=1)

    if text != original:
        path.write_text(text)
    return changes


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Update recipe/meta.yaml from Microsoft's .NET release metadata."
    )
    # Not required: --list-channels needs no channel at all.
    src = p.add_mutually_exclusive_group(required=False)
    src.add_argument("--channel", help='release channel, e.g. "10.0"')
    src.add_argument("--sdk-version", help='exact SDK version, e.g. "10.0.302"')
    # Relative to the CWD, not to this script: the script lives outside the
    # feedstock (a conda-forge feedstock's .gitignore ignores every root file
    # except conda-forge.yml, so it cannot be hosted there), and the natural
    # invocation is from a feedstock checkout.
    p.add_argument(
        "--recipe",
        type=Path,
        default=Path("recipe/meta.yaml"),
        help="path to meta.yaml, relative to CWD (default: recipe/meta.yaml)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve versions and URLs but download nothing",
    )
    p.add_argument("--write", action="store_true", help="rewrite meta.yaml in place")
    p.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if meta.yaml differs from upstream (for CI); writes nothing",
    )
    p.add_argument(
        "--probe",
        action="store_true",
        help=(
            "cheap staleness check: compare versions only, download nothing. "
            "Exit 0 = up to date, 10 = update available, 1 = error. Use this to "
            "gate the expensive hashing step in scheduled automation."
        ),
    )
    p.add_argument(
        "--summary-json",
        type=Path,
        help="write release facts (versions, security flag, CVE ids) here as JSON",
    )
    p.add_argument(
        "--list-channels",
        action="store_true",
        help=(
            "print every channel Microsoft publishes as JSON (channel, "
            "support-phase, release-type, latest-release, latest-sdk) and exit. "
            "Pure data -- policy lives in channels.json, applied by plan.py."
        ),
    )
    p.add_argument(
        "--list-rids",
        action="store_true",
        help=(
            "print the SDK RIDs Microsoft publishes for --channel as JSON and "
            "exit. Used to audit offered architectures against packaged ones."
        ),
    )
    p.add_argument(
        "--no-reset-build-number",
        action="store_true",
        help="keep build.number instead of resetting to 0 on a version change",
    )
    args = p.parse_args(argv)

    # Discovery mode: no channel needed, emit the whole index and stop.
    if args.list_channels:
        index = fetch_json(INDEX_URL)
        print(
            json.dumps(
                [
                    {
                        "channel": e.get("channel-version"),
                        "support_phase": e.get("support-phase"),
                        "release_type": e.get("release-type"),
                        "latest_release": e.get("latest-release"),
                        "latest_release_date": e.get("latest-release-date"),
                        "latest_sdk": e.get("latest-sdk"),
                        "eol_date": e.get("eol-date"),
                    }
                    for e in index.get("releases-index", [])
                ],
                indent=2,
            )
        )
        return 0

    if not args.channel and not args.sdk_version:
        p.error("one of --channel / --sdk-version is required (or use --list-channels)")

    channel = args.channel
    if channel is None:
        # "10.0.302" -> "10.0"
        channel = ".".join(args.sdk_version.split(".")[:2])
    # Channel and version strings reach a shell in CI, and both originate in
    # upstream JSON. Validate them here rather than trusting the caller.
    if not CHANNEL_RE.match(channel):
        sys.exit(f"error: refusing implausible channel {channel!r}")
    if args.sdk_version and not SDK_RE.match(args.sdk_version):
        sys.exit(f"error: refusing implausible sdk version {args.sdk_version!r}")

    log(f"fetching release index for channel {channel}...")
    index = fetch_json(INDEX_URL)
    entry = find_channel(index, channel)
    log(
        f"  channel {channel}: latest-release={entry.get('latest-release')} "
        f"latest-sdk={entry.get('latest-sdk')} "
        f"support-phase={entry.get('support-phase')} "
        f"({entry.get('release-type')})"
    )
    if entry.get("support-phase") == "eol":
        log(f"  warning: channel {channel} is end-of-life")

    releases = fetch_json(entry["releases.json"]).get("releases", [])
    release = pick_release(releases, args.sdk_version)
    sdk = select_sdk(release, args.sdk_version)

    sdk_version = sdk["version"]
    runtime_version = release["runtime"]["version"]
    for label, value in (("sdk", sdk_version), ("runtime", runtime_version)):
        if not SDK_RE.match(str(value)):
            sys.exit(
                f"error: upstream {label} version {value!r} contains unexpected "
                "characters; refusing to use it"
            )
    log(
        f"  release {release.get('release-version')} ({release.get('release-date')}): "
        f"sdk={sdk_version} runtime={runtime_version}"
    )
    if release.get("security"):
        # The metadata key is "cve-id", not "id".
        cves = [c.get("cve-id") for c in release.get("cve-list", [])]
        cves = [c for c in cves if c]
        log(
            f"  note: security release -- {len(cves)} CVE(s): "
            + (", ".join(cves) if cves else "ids not listed")
        )

    if args.list_rids:
        # Every RID this SDK publishes an archive for, with the archive name so a
        # caller can tell a .tar.gz/.zip from an installer-only RID.
        rids: dict[str, list[str]] = {}
        for f in sdk.get("files", []):
            rids.setdefault(f.get("rid", "?"), []).append(f.get("name", "?"))
        print(
            json.dumps(
                {
                    "channel": channel,
                    "sdk_version": sdk_version,
                    "rids": {
                        rid: sorted(names) for rid, names in sorted(rids.items())
                    },
                    "archive_rids": sorted(
                        rid
                        for rid, names in rids.items()
                        if any(n.endswith((".tar.gz", ".zip")) for n in names)
                    ),
                },
                indent=2,
            )
        )
        return 0

    if args.summary_json:
        args.summary_json.write_text(
            json.dumps(
                {
                    "channel": channel,
                    "sdk_version": sdk_version,
                    "runtime_version": runtime_version,
                    "release_version": release.get("release-version"),
                    "release_date": release.get("release-date"),
                    "support_phase": entry.get("support-phase"),
                    "release_type": entry.get("release-type"),
                    "security": bool(release.get("security")),
                    "cves": [
                        c.get("cve-id")
                        for c in release.get("cve-list", [])
                        if c.get("cve-id")
                    ],
                    "release_notes": release.get("release-notes"),
                },
                indent=2,
            )
            + "\n"
        )
        log(f"  wrote {args.summary_json}")

    # Probe before doing anything expensive. Versions alone are enough to decide
    # whether a bump exists, and they cost two HTTP GETs instead of ~1 GB -- which
    # is what makes a frequent cron affordable.
    if args.probe:
        if not args.recipe.exists():
            sys.exit(f"error: {args.recipe} not found (run from a feedstock checkout?)")
        text = args.recipe.read_text()
        cur_sdk = re.search(r'set\s+sdk_version\s*=\s*"([^"]*)"', text)
        cur_rt = re.search(r'set\s+runtime_version\s*=\s*"([^"]*)"', text)
        cur_sdk = cur_sdk.group(1) if cur_sdk else "?"
        cur_rt = cur_rt.group(1) if cur_rt else "?"
        log(f"  recipe has sdk={cur_sdk} runtime={cur_rt}")
        if (cur_sdk, cur_rt) == (sdk_version, runtime_version):
            print(f"up-to-date {sdk_version}")
            return 0
        print(f"update-available {cur_sdk}->{sdk_version} {cur_rt}->{runtime_version}")
        return 10

    platforms = platforms_for(args.recipe)
    log(
        "  recipe shape: "
        + ", ".join(f"{rid}->[{sel}]" for sel, rid, _e in platforms)
    )
    artifacts = {
        sel: find_artifact(sdk, rid, ext) for sel, rid, ext in platforms
    }

    if args.dry_run:
        log("\ndry run -- resolved artifacts, nothing downloaded:")
        for sel, _rid, _ext in platforms:
            a = artifacts[sel]
            print(f"# [{sel}]\n  {a['url']}\n  published sha512: {a.get('hash','<none>')[:24]}...")
        sys.stdout.flush()  # keep ordering sane when stderr and stdout share a tty
        log(
            "\nRun without --dry-run to download and hash "
            f"{len(platforms)} artifacts (~1 GB of transfer)."
        )
        return 0

    hashes: dict[str, str] = {}
    for i, (sel, _rid, _ext) in enumerate(platforms, 1):
        a = artifacts[sel]
        log(f"[{i}/{len(platforms)}] {a['name']} ({sel})")
        hashes[sel] = stream_hashes(a["url"], a.get("hash"))
        log(f"    sha256 {hashes[sel]}  (sha512 verified)")

    block = render_block(sdk_version, runtime_version, hashes, platforms)

    if args.check:
        text = args.recipe.read_text()
        stale = [
            f"sdk_version should be {sdk_version}"
            if f'set sdk_version = "{sdk_version}"' not in text
            else None,
            f"runtime_version should be {runtime_version}"
            if f'set runtime_version = "{runtime_version}"' not in text
            else None,
        ]
        stale += [
            f"sha256 for [{sel}] is stale"
            for sel in hashes
            if hashes[sel] not in text
        ]
        stale = [s for s in stale if s]
        if stale:
            print(f"{args.recipe} is out of date:", file=sys.stderr)
            for s in stale:
                print(f"  - {s}", file=sys.stderr)
            print("\n" + block)
            return 1
        log(f"{args.recipe} is up to date with {sdk_version}")
        return 0

    if args.write:
        changes = rewrite_meta(
            args.recipe,
            sdk_version,
            runtime_version,
            hashes,
            reset_build_number=not args.no_reset_build_number,
        )
        if changes:
            log(f"\nupdated {args.recipe}:")
            for c in changes:
                log(f"  {c}")
            log("\nNext: `conda smithy rerender`, then commit.")
        else:
            log(f"\n{args.recipe} already up to date -- no changes written.")
        return 0

    print(block)
    return 0


if __name__ == "__main__":
    sys.exit(main())
