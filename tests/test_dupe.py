"""Tests for find_duplicate_pr.py.

Guards the specific near-miss this exists for: the bot names its branches
`autobump/<sdk>`, so a branch-existence check alone would have opened a duplicate
of conda-forge/dotnet-feedstock#116, which proposed 10.0.302 against `main` from
a human's `v10update` branch.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture(scope="module")
def dupe():
    spec = importlib.util.spec_from_file_location("dupe", SCRIPTS / "find_duplicate_pr.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Shaped exactly like the real #116.
PR116 = {"number": 116, "title": "10.0.302 / runtime 10.0.10 ; win-arm64",
         "headRefName": "v10update"}


def test_detects_a_human_pr_by_version_in_the_title(dupe):
    """The #116 case: our branch name would not match, but the version does."""
    assert dupe.find([PR116], "10.0.302") == "#116 (v10update)"


def test_detects_our_own_branch_by_name(dupe):
    prs = [{"number": 9, "title": "something else", "headRefName": "autobump/8.0.423"}]
    assert dupe.find(prs, "8.0.423") == "#9 (autobump/8.0.423)"


def test_ignores_an_unrelated_version(dupe):
    assert dupe.find([PR116], "10.0.999") is None


def test_no_open_prs(dupe):
    assert dupe.find([], "10.0.302") is None


def test_a_different_line_is_not_a_duplicate(dupe):
    """8.0.423 must not be shadowed by an open 10.0.302 PR."""
    assert dupe.find([PR116], "8.0.423") is None


def test_returns_the_first_match_only(dupe):
    prs = [PR116, {"number": 200, "title": "10.0.302 again", "headRefName": "other"}]
    assert dupe.find(prs, "10.0.302") == "#116 (v10update)"


def test_exit_codes(dupe, monkeypatch, capsys):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO('[{"number":1,"title":"10.0.302","headRefName":"x"}]'))
    assert dupe.main(["prog", "10.0.302"]) == 0, "found -> 0"
    monkeypatch.setattr("sys.stdin", io.StringIO("[]"))
    assert dupe.main(["prog", "10.0.302"]) == 1, "not found -> 1"
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert dupe.main(["prog", "10.0.302"]) == 2, "bad input -> 2, distinct from 'not found'"
