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


# --------------------------------------------------------------------------
# version matching is boundary-anchored, not a plain substring
#
# The regression: a substring test made `8.0.42` match a PR titled `8.0.423`,
# which would silently skip a legitimate bump. .NET patch numbers are three
# digits today so it could not bite yet, but the failure is invisible.
# --------------------------------------------------------------------------


def test_a_shorter_version_does_not_match_a_longer_one(dupe):
    """8.0.42 must NOT match a PR proposing 8.0.423."""
    prs = [{"number": 1, "title": "8.0.423 / runtime 8.0.29", "headRefName": "autobump/8.0.423"}]
    assert dupe.find(prs, "8.0.42") is None
    assert dupe.find(prs, "8.0.423") == "#1 (autobump/8.0.423)"


def test_a_longer_version_does_not_match_a_shorter_one(dupe):
    prs = [{"number": 1, "title": "8.0.4 released", "headRefName": "autobump/8.0.4"}]
    assert dupe.find(prs, "8.0.42") is None


@pytest.mark.parametrize(
    "text,sdk,expected",
    [
        ("8.0.423 / runtime 8.0.29", "8.0.423", True),      # start of string
        ("autobump/8.0.423", "8.0.423", True),               # after a slash
        ("bump to 8.0.423.", "8.0.423", False),              # trailing dot -> a longer version
        ("v8.0.423", "8.0.423", False),                      # glued to a word char
        ("8.0.4231", "8.0.423", False),                       # glued to a digit
        ("(8.0.423)", "8.0.423", True),                       # bracketed
        ("8.0.423", "8.0.423", True),                          # exact
        ("10.0.302 / runtime 10.0.10 ; win-arm64", "10.0.302", True),  # the real #116 title
    ],
)
def test_boundary_cases(dupe, text, sdk, expected):
    assert dupe.mentions(text, sdk) is expected


def test_regex_metacharacters_in_the_version_are_escaped(dupe):
    """Dots are literal, so 8x0x423 must not match 8.0.423."""
    assert not dupe.mentions("8x0x423", "8.0.423")


def test_the_real_116_case_still_works(dupe):
    """Regression guard: the case this check exists for."""
    prs = [{"number": 116, "title": "10.0.302 / runtime 10.0.10 ; win-arm64",
            "headRefName": "v10update"}]
    assert dupe.find(prs, "10.0.302") == "#116 (v10update)"
    assert dupe.find(prs, "10.0.30") is None
