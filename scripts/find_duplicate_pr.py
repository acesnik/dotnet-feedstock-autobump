#!/usr/bin/env python3
"""Is this SDK version already proposed by an open PR?

    gh pr list --repo <upstream> --base <branch> --state open --limit 50 \
        --json number,title,headRefName | find_duplicate_pr.py <sdk-version>

Prints a short identifier and exits 0 if a duplicate is found, prints nothing and
exits 1 otherwise (so `if ... ; then` reads naturally in the workflow).

Why this exists: branch-existence is not a sufficient check. The bot names its
branches `autobump/<sdk>`, but a human proposing the same bump uses their own
name -- conda-forge/dotnet-feedstock#116 proposed 10.0.302 against `main` from
`v10update`. Checking only for `autobump/10.0.302` would have opened a duplicate
competing PR.

A separate file rather than inline in the workflow because embedding multi-line
Python in a YAML block scalar broke the workflow parse twice, and because this
way it can be tested.
"""

from __future__ import annotations

import json
import sys


def find(prs: list[dict], sdk: str) -> str | None:
    """First open PR whose title or head branch mentions this SDK version.

    Deliberately a substring test on the version rather than anything cleverer.
    A version like `10.0.302` is specific enough that a mention in either place
    means someone is already proposing it, and the cost of a false positive
    (skipping a run, logging why) is far lower than opening a duplicate PR
    against a shared feedstock.
    """
    for pr in prs:
        title = str(pr.get("title", ""))
        head = str(pr.get("headRefName", ""))
        if sdk in title or sdk in head:
            return f"#{pr.get('number')} ({head})"
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        prs = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"error: could not parse PR list: {exc}", file=sys.stderr)
        return 2
    if not isinstance(prs, list):
        print("error: expected a JSON array from `gh pr list`", file=sys.stderr)
        return 2
    hit = find(prs, argv[1])
    if hit:
        print(hit)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
