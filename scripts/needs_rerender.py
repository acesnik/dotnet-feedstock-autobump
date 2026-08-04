#!/usr/bin/env python3
"""Which open autobump PRs still need a rerender?

    gh pr list --repo <upstream> --state open --limit 100 \
        --json number,headRefName | needs_rerender.py --candidates
    gh pr view <n> --json commits | needs_rerender.py --check

Two stages on purpose. Asking `gh pr list` for `commits` across 50 PRs blows
GitHub's GraphQL node budget outright:

    GraphQL: This query requests up to 505,050 possible nodes which exceeds the
    maximum limit of 500,000.

So stage one filters on cheap fields, and stage two fetches commits only for the
handful of PRs that are actually candidates.

Stateless by design. The alternative -- having the bump job tell this step which
PRs it just opened -- cannot see a PR whose rerender failed in an earlier run, and
that is exactly the case worth handling: conda-forge/dotnet-feedstock#119's
rerender died with `OSError 39, Directory not empty` and sat there needing a
human to notice. Deriving the list from the PRs themselves makes each run
self-healing.

A rerender is detected by a commit whose message starts with `MNT: Re-rendered`,
which is what conda-smithy writes.
"""

from __future__ import annotations

import json
import sys

RERENDER_PREFIX = "mnt: re-rendered"


def already_rerendered(pr: dict) -> bool:
    for c in pr.get("commits") or []:
        head = str(c.get("messageHeadline", "")).strip().lower()
        if head.startswith(RERENDER_PREFIX):
            return True
    return False


def select(prs: list[dict], prefix: str = "autobump/") -> list[int]:
    """Open PRs from our own branches that carry no rerender commit.

    Restricted to our branch prefix so a human's in-progress PR is never poked
    with an automated rerender request they did not ask for.
    """
    out = []
    for pr in prs:
        if not str(pr.get("headRefName", "")).startswith(prefix):
            continue
        if already_rerendered(pr):
            continue
        out.append(int(pr["number"]))
    return sorted(out)


def candidates(prs: list[dict], prefix: str = "autobump/") -> list[int]:
    """Stage one: our own open PRs, by branch prefix. No commit data needed.

    Restricted to our prefix so a human's in-progress PR is never poked with an
    automated rerender request they did not ask for.
    """
    return sorted(
        int(pr["number"])
        for pr in prs
        if str(pr.get("headRefName", "")).startswith(prefix)
    )


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "--candidates"
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"error: could not parse input: {exc}", file=sys.stderr)
        return 2

    if mode == "--check":
        # Stage two: one PR's commits. Exit 0 if it still needs a rerender.
        if not isinstance(data, dict):
            print("error: --check expects a single PR object", file=sys.stderr)
            return 2
        return 1 if already_rerendered(data) else 0

    if not isinstance(data, list):
        print("error: expected a JSON array from `gh pr list`", file=sys.stderr)
        return 2
    prefix = argv[2] if len(argv) > 2 else "autobump/"
    for n in candidates(data, prefix):
        print(n)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
