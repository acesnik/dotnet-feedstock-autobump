#!/usr/bin/env python3
"""Render a PR body from the release summary written by update-dotnet-version.py.

Usage: render_pr_body.py summary.json > body.md
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

NOTES_FALLBACK = (
    "https://github.com/dotnet/core/blob/main/release-notes/"
    "{channel}/{release}/{release}.md"
)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    d = json.loads(Path(argv[1]).read_text())

    sdk = d["sdk_version"]
    runtime = d["runtime_version"]
    notes = d.get("release_notes") or NOTES_FALLBACK.format(
        channel=d["channel"], release=d.get("release_version", runtime)
    )

    out: list[str] = []
    out.append(
        f"Updates the {d['channel']} line to `{sdk}` / runtime `{runtime}` "
        f"({d.get('release_type', '').upper()}, released {d.get('release_date', 'n/a')})."
    )
    out.append("")

    cves = d.get("cves") or []
    if d.get("security"):
        out.append(
            f"**This is a security release"
            + (f" covering {len(cves)} CVE(s):**" if cves else ":**")
        )
        if cves:
            out.append("")
            out.append(", ".join(f"`{c}`" for c in cves))
        out.append("")

    out.append(f"Release notes: {notes}")
    out.append("")
    out.append("### How the hashes were produced")
    out.append("")
    out.append(
        "Generated from Microsoft's published release metadata "
        "(`release-metadata/{}/releases.json`) rather than by hand. Each artifact "
        "is streamed once, computing SHA-256 while verifying Microsoft's published "
        "SHA-512 in the same pass — so the hashes derive from bytes provably "
        "matching the release manifest, and a truncated or substituted download "
        "fails loudly instead of yielding a plausible-looking hash.".format(
            d["channel"]
        )
    )
    out.append("")
    out.append(
        "The metadata publishes SHA-512 and conda-build accepts only "
        "md5/sha1/sha256, which is why re-hashing is necessary at all (see #90)."
    )
    out.append("")
    out.append("### Notes for review")
    out.append("")
    out.append(
        "- Opened automatically. `build.number` is reset to 0 only when the "
        "version actually changed."
    )
    out.append(
        "- A rerender has been requested in a follow-up comment; the bot pushes "
        "it as its own commit so it stays separate from the version bump."
    )
    out.append(
        "- The two independent version variables (`sdk_version` vs "
        "`runtime_version`) are read from the same release entry, so they cannot "
        "drift apart here — that pairing is what the generic autotick bot cannot "
        "express (see #55)."
    )
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
