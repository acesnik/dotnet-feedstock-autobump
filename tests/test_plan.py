"""Tests for plan.py's decision logic.

Each of these corresponds to a decision I verified once by hand while building
this. They exist so a refactor cannot silently undo any of them -- particularly
the negative cases, where the correct behaviour is to do *nothing*, and a
regression therefore looks exactly like success.
"""

from __future__ import annotations

import json

import pytest

from conftest import channel


def cfg(base, **over):
    return dict(base, **over)


def plan_it(plan, config, channels, feedstock):
    bumps, issues, lines, notices, transition = plan.plan_lines(
        config, channels, feedstock
    )
    return {
        "bumps": bumps,
        "issue_keys": [i["key"] for i in issues],
        "lines": lines,
        "notices": notices,
        "transition": transition,
    }


# --------------------------------------------------------------------------
# bumping tracked lines
# --------------------------------------------------------------------------


def test_stale_line_is_bumped_against_its_own_branch(plan, real_config, feedstock):
    r = plan_it(
        plan,
        cfg(real_config, tracked={"8.0": "v8"}),
        [channel("8.0", "maintenance", "lts", "8.0.423")],
        feedstock,
    )
    assert [(b["channel"], b["branch"]) for b in r["bumps"]] == [("8.0", "v8")]
    assert r["bumps"][0]["current_sdk"] == "8.0.407"


def test_current_line_is_not_bumped(plan, real_config, feedstock):
    r = plan_it(
        plan,
        cfg(real_config, tracked={"8.0": "v8"}),
        [channel("8.0", "maintenance", "lts", "8.0.407")],
        feedstock,
    )
    assert r["bumps"] == []
    assert r["issue_keys"] == []


def test_each_line_bumps_independently(plan, real_config, feedstock):
    """The whole point of per-branch bumps: an 8.0 bump must not touch main."""
    r = plan_it(
        plan,
        cfg(real_config, tracked={"10.0": "main", "9.0": "v9", "8.0": "v8"}),
        [
            channel("10.0", "active", "lts", "10.0.302"),
            channel("9.0", "maintenance", "sts", "9.0.316"),
            channel("8.0", "maintenance", "lts", "8.0.423"),
        ],
        feedstock,
    )
    assert sorted((b["channel"], b["branch"]) for b in r["bumps"]) == [
        ("10.0", "main"),
        ("8.0", "v8"),
        ("9.0", "v9"),
    ]


def test_missing_branch_escalates(plan, real_config, feedstock):
    r = plan_it(
        plan,
        cfg(real_config, tracked={"7.0": "v7-nope"}),
        [channel("7.0", "maintenance", "sts", "7.0.410")],
        feedstock,
    )
    assert r["issue_keys"] == ["missing-branch-v7-nope"]
    assert r["bumps"] == []


def test_channel_vanishing_from_the_index_escalates(plan, real_config, feedstock):
    r = plan_it(plan, cfg(real_config, tracked={"8.0": "v8"}), [], feedstock)
    assert r["issue_keys"] == ["channel-gone-8.0"]


def test_branch_cut_from_the_wrong_line_is_noticed(plan, real_config, feedstock):
    """v9 carrying a 10.x recipe means it was cut from the wrong commit.

    A notice, not an issue: it is often benign, because the recipe is
    version-agnostic and a branch cut from a newer main inherits newer
    infrastructure. But it must be visible.
    """
    r = plan_it(
        plan,
        cfg(real_config, tracked={"9.0": "main"}),  # main ships 10.0.100
        [channel("9.0", "maintenance", "sts", "9.0.316")],
        feedstock,
    )
    assert any("tracked for 9.0 but its recipe says 10.0.100" in n for n in r["notices"])


# --------------------------------------------------------------------------
# refusing to bump what cannot be packaged
# --------------------------------------------------------------------------


def test_preview_line_in_tracked_is_refused_not_bumped(plan, real_config, feedstock):
    r = plan_it(
        plan,
        cfg(real_config, tracked={"11.0": "main"}),
        [channel("11.0", "preview", "sts", "11.0.100-preview.6.26359.118")],
        feedstock,
    )
    assert r["bumps"] == []
    assert r["issue_keys"] == ["preview-tracked-11.0"]


def test_unpackageable_version_on_an_active_line_is_refused(plan, real_config, feedstock):
    """The guard is general, not preview-specific: an rc on a GA line too."""
    r = plan_it(
        plan,
        cfg(real_config, tracked={"10.0": "main"}),
        [channel("10.0", "active", "lts", "10.0.400-rc.1")],
        feedstock,
    )
    assert r["bumps"] == []
    assert r["issue_keys"] == ["unpackageable-version-10.0-10.0.400-rc.1"]


# --------------------------------------------------------------------------
# end of life
# --------------------------------------------------------------------------


def test_eol_line_below_its_final_version_gets_a_final_bump_issue(
    plan, real_config, feedstock
):
    r = plan_it(
        plan,
        cfg(real_config, tracked={"8.0": "v8"}),
        [channel("8.0", "eol", "lts", "8.0.999")],
        feedstock,
    )
    assert r["bumps"] == [], "EOL lines must never be bumped automatically"
    assert r["issue_keys"] == ["eol-final-bump-8.0"]


def test_eol_line_already_final_is_silent(plan, real_config, feedstock):
    r = plan_it(
        plan,
        cfg(real_config, tracked={"8.0": "v8"}),
        [channel("8.0", "eol", "lts", "8.0.407")],
        feedstock,
    )
    assert r["bumps"] == [] and r["issue_keys"] == []


# --------------------------------------------------------------------------
# lines we do not track
# --------------------------------------------------------------------------


def test_supported_untracked_line_escalates_as_a_maintenance_gap(
    plan, real_config, feedstock
):
    """The 9.0 orphaning: supported upstream, no branch, silently rotting."""
    r = plan_it(
        plan,
        cfg(real_config, tracked={"10.0": "main"}),
        [
            channel("10.0", "active", "lts", "10.0.100"),
            channel("9.0", "maintenance", "sts", "9.0.316"),
        ],
        feedstock,
    )
    assert r["issue_keys"] == ["untracked-supported-9.0"]


def test_eol_untracked_line_is_ignored(plan, real_config, feedstock):
    r = plan_it(
        plan,
        cfg(real_config, tracked={"10.0": "main"}),
        [
            channel("10.0", "active", "lts", "10.0.100"),
            channel("6.0", "eol", "lts", "6.0.428"),
        ],
        feedstock,
    )
    assert r["issue_keys"] == []


# --------------------------------------------------------------------------
# policy and the transition
# --------------------------------------------------------------------------


GA11 = [
    channel("11.0", "active", "sts", "11.0.100"),
    channel("10.0", "active", "lts", "10.0.100"),
]


def test_ga_line_triggers_a_transition_under_policy_latest(plan, real_config, feedstock):
    r = plan_it(plan, cfg(real_config, tracked={"10.0": "main"}), GA11, feedstock)
    t = r["transition"]
    assert t["from_channel"] == "10.0" and t["from_branch"] == "main"
    assert t["to_channel"] == "11.0" and t["cut_branch"] == "v10"
    assert r["issue_keys"] == [], "a transition supersedes the judgment-call issue"


def test_sts_line_under_policy_lts_is_a_notice_not_a_transition(
    plan, real_config, feedstock
):
    r = plan_it(
        plan,
        cfg(real_config, tracked={"10.0": "main"}, policy="lts"),
        GA11,
        feedstock,
    )
    assert r["transition"] is None
    assert r["issue_keys"] == []
    assert any("policy is `lts`" in n for n in r["notices"])


def test_lts_line_under_policy_lts_does_transition(plan, real_config, feedstock):
    r = plan_it(
        plan,
        cfg(real_config, tracked={"10.0": "main"}, policy="lts"),
        [
            channel("12.0", "active", "lts", "12.0.100"),
            channel("10.0", "active", "lts", "10.0.100"),
        ],
        feedstock,
    )
    assert r["transition"]["to_channel"] == "12.0"


def test_policy_manual_never_adopts(plan, real_config, feedstock):
    r = plan_it(
        plan,
        cfg(real_config, tracked={"10.0": "main"}, policy="manual"),
        GA11,
        feedstock,
    )
    assert r["transition"] is None and r["issue_keys"] == []
    assert any("`manual`" in n for n in r["notices"])


def test_preview_line_never_transitions(plan, real_config, feedstock):
    r = plan_it(
        plan,
        cfg(real_config, tracked={"10.0": "main"}),
        [
            channel("11.0", "preview", "sts", "11.0.100-preview.6.26359.118"),
            channel("10.0", "active", "lts", "10.0.100"),
        ],
        feedstock,
    )
    assert r["transition"] is None
    assert any("preview" in n for n in r["notices"])


def test_transition_suppressed_when_the_cut_branch_exists(plan, real_config, feedstock):
    # 9.0 on main would cut v9, which the fixture repo already has.
    r = plan_it(
        plan,
        cfg(real_config, tracked={"9.0": "main"}),
        [
            channel("11.0", "active", "sts", "11.0.100"),
            channel("9.0", "active", "sts", "9.0.316"),
        ],
        feedstock,
    )
    assert r["transition"] is None
    assert any("already exists" in n for n in r["notices"])


def test_transition_disabled_falls_back_to_an_issue(plan, real_config, feedstock):
    r = plan_it(
        plan,
        cfg(real_config, tracked={"10.0": "main"}, transition={"enabled": False}),
        GA11,
        feedstock,
    )
    assert r["transition"] is None
    assert r["issue_keys"] == ["new-channel-11.0"]


def test_no_transition_when_the_outgoing_line_is_not_on_main(
    plan, real_config, feedstock
):
    """Only `main` hands over; a vN branch cannot be the outgoing line."""
    r = plan_it(
        plan,
        cfg(real_config, tracked={"8.0": "v8"}),
        [channel("11.0", "active", "sts", "11.0.100"), channel("8.0", "active", "lts", "8.0.407")],
        feedstock,
    )
    assert r["transition"] is None
    assert r["issue_keys"] == ["new-channel-11.0"]


def test_outgoing_bump_is_deferred_during_a_transition(plan, real_config, feedstock):
    """Both would target main and conflict, so the outgoing bump waits."""
    r = plan_it(
        plan,
        cfg(real_config, tracked={"10.0": "main", "8.0": "v8"}),
        [
            channel("11.0", "active", "sts", "11.0.100"),
            channel("10.0", "active", "lts", "10.0.302"),   # stale
            channel("8.0", "maintenance", "lts", "8.0.423"),  # stale, different branch
        ],
        feedstock,
    )
    assert r["transition"] is not None
    channels_bumped = [b["channel"] for b in r["bumps"]]
    assert "10.0" not in channels_bumped, "outgoing line must not bump during transition"
    assert "8.0" in channels_bumped, "unrelated lines still bump"
    assert any("Deferred the 10.0 bump" in n for n in r["notices"])


# --------------------------------------------------------------------------
# architecture audit
# --------------------------------------------------------------------------


@pytest.fixture
def sized(monkeypatch, plan):
    """Stub repodata sizes; None means the subdir does not exist."""

    def apply(mapping):
        monkeypatch.setattr(plan, "repodata_size", lambda sub: mapping.get(sub))

    return apply


PACKAGED = {"linux-x64", "linux-arm64", "osx-x64", "osx-arm64", "win-x64", "win-arm64"}


def test_dropped_rid_escalates_as_impending_breakage(plan, real_config, sized):
    sized({})
    offered = sorted(PACKAGED - {"win-arm64"})
    issues, _ = plan.plan_rids(real_config, offered, PACKAGED)
    assert [i["key"] for i in issues] == ["rid-dropped-win-arm64"]


def test_viable_new_rid_is_offered_as_a_candidate(plan, real_config, sized):
    sized({"win-32": 10_000_000})
    cfg2 = dict(real_config, active_subdirs=real_config["active_subdirs"] + ["win-32"])
    issues, _ = plan.plan_rids(cfg2, sorted(PACKAGED | {"win-x86"}), PACKAGED)
    assert [i["key"] for i in issues] == ["rid-available-win-x86"]


def test_frozen_subdir_is_skipped_even_though_it_is_huge(plan, real_config, sized):
    """win-32 has 24k packages but conda-forge stopped building it in 2019.

    Size alone cannot tell 'thriving' from 'frozen', which is why
    active_subdirs is a declared allowlist rather than inferred.
    """
    sized({"win-32": 10_000_000})
    issues, skipped = plan.plan_rids(real_config, sorted(PACKAGED | {"win-x86"}), PACKAGED)
    assert issues == []
    assert any(s["rid"] == "win-x86" and "not actively built" in s["reason"] for s in skipped)


def test_subdir_without_an_ecosystem_is_skipped(plan, real_config, sized):
    """linux-armv7l exists but has 3 packages -- nothing to depend on."""
    sized({"linux-armv7l": 1306})
    cfg2 = dict(
        real_config, active_subdirs=real_config["active_subdirs"] + ["linux-armv7l"]
    )
    issues, skipped = plan.plan_rids(cfg2, sorted(PACKAGED | {"linux-arm"}), PACKAGED)
    assert issues == []
    assert any(s["rid"] == "linux-arm" and "no ecosystem" in s["reason"] for s in skipped)


def test_musl_rids_are_ignored_quietly(plan, real_config, sized):
    sized({})
    issues, skipped = plan.plan_rids(
        real_config, sorted(PACKAGED | {"linux-musl-x64"}), PACKAGED
    )
    assert issues == []
    assert any(
        s["rid"] == "linux-musl-x64" and "no conda-forge equivalent" in s["reason"]
        for s in skipped
    )


def test_steady_state_is_silent(plan, real_config, sized):
    sized({})
    issues, _ = plan.plan_rids(real_config, sorted(PACKAGED), PACKAGED)
    assert issues == []


def test_packaged_rids_come_from_the_updater_not_config(plan, updater, real_config):
    """The audit must read PLATFORMS, or it can drift from the actual recipe."""
    assert "packaged_rids" not in real_config
    assert {rid for _s, rid, _e in updater.PLATFORMS} == PACKAGED


# --------------------------------------------------------------------------
# issue hygiene
# --------------------------------------------------------------------------


def test_issue_keys_are_unique_and_stable(plan, real_config, feedstock):
    r = plan_it(
        plan,
        cfg(real_config, tracked={"10.0": "main"}),
        [
            channel("10.0", "active", "lts", "10.0.100"),
            channel("9.0", "maintenance", "sts", "9.0.316"),
            channel("8.0", "maintenance", "lts", "8.0.423"),
        ],
        feedstock,
    )
    keys = r["issue_keys"]
    assert len(keys) == len(set(keys)), "duplicate keys would defeat dedup"
    assert all(k == k.strip() and " " not in k for k in keys)


# --------------------------------------------------------------------------
# per-line architecture audit
#
# Auditing only the newest line and applying the verdict everywhere was the
# original review's finding #6. Each line has its own recipe AND its own set of
# published RIDs, so a conclusion drawn from 10.0 can be false for 8.0.
# --------------------------------------------------------------------------

LINES = [
    {"channel": "10.0", "branch": "main"},
    {"channel": "9.0", "branch": "v9"},
    {"channel": "8.0", "branch": "v8"},
]
NEWEST = LINES[0]


def test_rid_dropped_from_an_older_line_only(plan):
    """8.0 stops publishing win-arm64 while 10.0 still does -> flag 8.0 only."""
    offered = lambda ch: (
        ["linux-x64", "win-x64"] if ch == "8.0" else ["linux-x64", "win-x64", "win-arm64"]
    )
    packaged = lambda br: {"linux-x64", "win-x64", "win-arm64"}
    issues = plan.per_line_rid_issues(LINES, NEWEST, offered, packaged)
    assert [i["key"] for i in issues] == ["rid-dropped-8.0-win-arm64"]
    assert "specific to this line" in issues[0]["body"]


def test_newest_line_is_left_to_the_main_audit(plan):
    """No duplicate finding for the newest line, which plan_rids already covers."""
    offered = lambda ch: ["linux-x64"]
    packaged = lambda br: {"linux-x64", "win-x64"}
    issues = plan.per_line_rid_issues(LINES, NEWEST, offered, packaged)
    assert all("10.0" not in i["key"] for i in issues)


def test_all_lines_consistent_is_silent(plan):
    offered = lambda ch: ["linux-x64", "win-x64"]
    packaged = lambda br: {"linux-x64", "win-x64"}
    assert plan.per_line_rid_issues(LINES, NEWEST, offered, packaged) == []


def test_an_older_line_packaging_less_is_not_a_problem(plan):
    """A line predating win-arm64 packages fewer RIDs -- that is normal."""
    offered = lambda ch: ["linux-x64", "win-x64", "win-arm64"]
    packaged = lambda br: {"linux-x64"} if br == "v8" else {"linux-x64", "win-arm64"}
    assert plan.per_line_rid_issues(LINES, NEWEST, offered, packaged) == []


def test_unreachable_metadata_is_not_treated_as_a_drop(plan):
    """An empty offered list means 'could not tell', not 'nothing published'."""
    offered = lambda ch: []
    packaged = lambda br: {"linux-x64", "win-x64"}
    assert plan.per_line_rid_issues(LINES, NEWEST, offered, packaged) == []


def test_dropped_keys_are_namespaced_per_line(plan):
    """Two lines dropping the same RID must not collide in issue dedup."""
    offered = lambda ch: ["linux-x64"]
    packaged = lambda br: {"linux-x64", "win-x64"}
    issues = plan.per_line_rid_issues(LINES, NEWEST, offered, packaged)
    keys = [i["key"] for i in issues]
    assert sorted(keys) == ["rid-dropped-8.0-win-x64", "rid-dropped-9.0-win-x64"]
    assert len(keys) == len(set(keys))


# --------------------------------------------------------------------------
# Ambiguous refs: a fork remote shadowing upstream
# --------------------------------------------------------------------------
def _repo_with_remote_refs(tmp_path, remotes: dict[str, str]):
    """A repo with two commits, and refs/remotes/<name>/main pointing at each.

    Built with `update-ref` rather than real remotes: the failure being tested is
    purely about which ref name wins, and real clones would make the fixture slow
    for no extra coverage.
    """
    import subprocess

    repo = tmp_path / "amb"
    (repo / "recipe").mkdir(parents=True)

    def run(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    run("symbolic-ref", "HEAD", "refs/heads/main")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")

    shas = {}
    for label in ("old", "new"):
        (repo / "recipe" / "meta.yaml").write_text(f"# {label}\n")
        run("add", "recipe/meta.yaml")
        run("commit", "-q", "-m", label)
        shas[label] = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    for remote, label in remotes.items():
        run("update-ref", f"refs/remotes/{remote}/main", shas[label])
    return repo, shas


def test_ambiguous_ref_detects_a_fork_shadowing_upstream(plan, tmp_path):
    """The real failure: `acesnik/main` was read instead of upstream's main.

    Remote ordering is alphabetical, so the fork won, and the tool reported the
    fork's stale recipe (10.0.100) as upstream's (10.0.302) -- which also made an
    already-packaged architecture look missing. Silent, and in the one place the
    tool is supposed to be authoritative.
    """
    repo, shas = _repo_with_remote_refs(
        tmp_path, {"acesnik": "old", "originDoNotPushHere": "new"}
    )
    amb = plan.ambiguous_ref(repo, "main")
    assert amb is not None
    chosen, resolved = amb
    assert chosen == "acesnik/main", "alphabetical order should still pick the fork"
    assert resolved["acesnik/main"] == shas["old"][:12]
    assert resolved["originDoNotPushHere/main"] == shas["new"][:12]


def test_agreeing_remotes_are_not_flagged(plan, tmp_path):
    """No false positive when every remote is at the same commit."""
    repo, _shas = _repo_with_remote_refs(tmp_path, {"a": "new", "b": "new"})
    assert plan.ambiguous_ref(repo, "main") is None


def test_single_remote_is_never_ambiguous(plan, feedstock):
    """CI checks out one repository, so this must stay quiet there."""
    assert plan.ambiguous_ref(feedstock, "main") is None


def test_bare_ref_is_tried_last(plan, feedstock):
    """A stale LOCAL branch must not shadow a remote one -- the original bug."""
    candidates = plan.git_candidates(feedstock, "v8")
    assert candidates[-1] == "v8"
    assert candidates[0] == "origin/v8"
    assert len(candidates) == len(set(candidates)), "candidates must be deduped"
