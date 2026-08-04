"""Tests for needs_rerender.py.

Exists because of a concrete incident: #118 and #119 had their rerenders
requested 1 second apart, one succeeded and one died with
`OSError 39, Directory not empty` inside conda-forge's rerender container. The
delay after PR creation was near-identical (19s vs 17s), so timing after the PR
was not the variable -- concurrency between two rerenders of the same feedstock
was. This module supports serialising them, and self-healing when one still fails.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture(scope="module")
def nr():
    spec = importlib.util.spec_from_file_location("nr", SCRIPTS / "needs_rerender.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def pr(n, head, *headlines):
    return {
        "number": n,
        "headRefName": head,
        "commits": [{"messageHeadline": h} for h in headlines],
    }


# --- stage one: candidates by branch prefix, no commit data ----------------


def test_only_our_own_branches_are_candidates(nr):
    """A human's PR must never be poked with an automated rerender request."""
    prs = [
        pr(201, "autobump/8.0.423", "bump"),
        pr(203, "v10update", "human work"),
        pr(204, "some-feature", "unrelated"),
    ]
    assert nr.candidates(prs) == [201]


def test_candidates_needs_no_commit_field(nr):
    """Stage one runs on the cheap query, which omits `commits` entirely.

    Asking gh for commits across 50 PRs exceeds GitHub's GraphQL node limit
    (505,050 > 500,000), which failed outright when first attempted.
    """
    prs = [{"number": 9, "headRefName": "autobump/1.2.3"}]
    assert nr.candidates(prs) == [9]


def test_candidates_sorted_and_deduped_by_number(nr):
    prs = [pr(300, "autobump/b"), pr(100, "autobump/a")]
    assert nr.candidates(prs) == [100, 300]


def test_custom_prefix(nr):
    prs = [pr(1, "autobump/x"), pr(2, "bot/x")]
    assert nr.candidates(prs, "bot/") == [2]


# --- stage two: does this PR still need a rerender? ------------------------


def test_pr_without_a_rerender_commit_needs_one(nr):
    assert not nr.already_rerendered(pr(1, "autobump/x", "8.0.423 / runtime 8.0.29"))


def test_pr_with_a_rerender_commit_does_not(nr):
    p = pr(1, "autobump/x", "8.0.423", "MNT: Re-rendered with conda-smithy 2026.6.14")
    assert nr.already_rerendered(p)


def test_rerender_detection_is_case_insensitive(nr):
    assert nr.already_rerendered(pr(1, "a", "mnt: re-rendered with conda-smithy"))


def test_a_rerender_anywhere_in_history_counts(nr):
    """The fix commit sits on top of the rerender, as on #118 and #119."""
    p = pr(1, "autobump/x", "bump", "MNT: Re-rendered", "Fix osx code signatures")
    assert nr.already_rerendered(p)


def test_a_commit_merely_mentioning_rerender_does_not_count(nr):
    """Only conda-smithy's own headline counts, not prose about it."""
    assert not nr.already_rerendered(pr(1, "a", "note: rerender did not help"))


def test_missing_commits_key_is_not_a_crash(nr):
    assert not nr.already_rerendered({"number": 1, "headRefName": "autobump/x"})


# --- CLI exit codes drive the workflow's `if` ------------------------------


def test_check_mode_exit_codes(nr, monkeypatch):
    import io

    needs = json.dumps(pr(1, "autobump/x", "bump"))
    monkeypatch.setattr("sys.stdin", io.StringIO(needs))
    assert nr.main(["prog", "--check"]) == 0, "needs rerender -> 0"

    done = json.dumps(pr(1, "autobump/x", "bump", "MNT: Re-rendered"))
    monkeypatch.setattr("sys.stdin", io.StringIO(done))
    assert nr.main(["prog", "--check"]) == 1, "already rerendered -> 1"


def test_bad_input_is_distinct_from_a_verdict(nr, monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert nr.main(["prog", "--check"]) == 2
    monkeypatch.setattr("sys.stdin", io.StringIO('{"a": 1}'))
    assert nr.main(["prog", "--candidates"]) == 2, "array expected"
