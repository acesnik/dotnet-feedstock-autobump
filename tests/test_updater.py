"""Tests for update-dotnet-version.py.

`rewrite_meta` gets the most attention here. It performs regex surgery on a
recipe where six sha256 lines are structurally identical and distinguished only
by a trailing selector comment, so a subtle anchoring bug would silently write
the wrong platform's hash -- a package that installs and then fails on exactly
one architecture. That is the worst failure mode in this repo, and the reason
these assertions exist rather than a one-off manual check.
"""

from __future__ import annotations

import json

import pytest


# --------------------------------------------------------------------------
# conda version validity
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "version,ok",
    [
        ("10.0.302", True),
        ("8.0.423", True),
        # Every Microsoft preview SDK looks like this, and conda-build rejects it:
        # package filenames are name-version-build, so a hyphen is ambiguous.
        ("11.0.100-preview.6.26359.118", False),
        ("10.0.400-rc.1", False),
        ("1.0.0 ", False),
        ("1.0.0!", False),
    ],
)
def test_conda_version_problem(updater, version, ok):
    assert (updater.conda_version_problem(version) is None) is ok


# --------------------------------------------------------------------------
# rewrite_meta: the selector-anchored surgery
# --------------------------------------------------------------------------

HASHES = {
    "linux and aarch64": "1" * 64,
    "linux and x86_64": "2" * 64,
    "osx and arm64": "3" * 64,
    "osx and x86_64": "4" * 64,
    "win and x86_64": "5" * 64,
    "win and arm64": "6" * 64,
}


def test_rewrite_meta_puts_each_hash_on_its_own_selector(updater, recipe):
    updater.rewrite_meta(recipe, "10.0.302", "10.0.10", HASHES, True)
    text = recipe.read_text()
    for selector, digest in HASHES.items():
        line = next(l for l in text.splitlines() if f"# [{selector}]" in l and "sha256" in l)
        assert digest in line, f"{selector} got the wrong hash"


def test_rewrite_meta_updates_both_versions(updater, recipe):
    updater.rewrite_meta(recipe, "10.0.302", "10.0.10", HASHES, True)
    text = recipe.read_text()
    assert 'set sdk_version = "10.0.302"' in text
    assert 'set runtime_version = "10.0.10"' in text


def test_rewrite_meta_preserves_structure(updater, recipe):
    before = recipe.read_text()
    updater.rewrite_meta(recipe, "10.0.302", "10.0.10", HASHES, True)
    after = recipe.read_text()
    assert len(before.splitlines()) == len(after.splitlines())
    assert before.count("# [") == after.count("# [")
    # The derived-framework line is Jinja and must not be touched.
    assert "{% set framework = '.'.join(sdk_version.split('.')[:2]) %}" in after


def test_rewrite_meta_resets_build_number_on_a_version_change(updater, recipe):
    changes = updater.rewrite_meta(recipe, "10.0.302", "10.0.10", HASHES, True)
    assert "number: 0" in recipe.read_text()
    assert any("build number" in c for c in changes)


def test_rewrite_meta_leaves_build_number_when_asked(updater, recipe):
    updater.rewrite_meta(recipe, "10.0.302", "10.0.10", HASHES, False)
    assert "number: 3" in recipe.read_text()


def test_rewrite_meta_is_idempotent(updater, recipe):
    updater.rewrite_meta(recipe, "10.0.302", "10.0.10", HASHES, True)
    once = recipe.read_text()
    changes = updater.rewrite_meta(recipe, "10.0.302", "10.0.10", HASHES, True)
    assert recipe.read_text() == once
    assert changes == [], "a no-op rewrite should report no changes"


def test_rewrite_meta_reports_what_changed(updater, recipe):
    changes = updater.rewrite_meta(recipe, "10.0.302", "10.0.10", HASHES, True)
    joined = " ".join(changes)
    assert "sdk_version: 10.0.100 -> 10.0.302" in joined
    assert "runtime_version: 10.0.0 -> 10.0.10" in joined
    for selector in HASHES:
        assert f"sha256 [{selector}]" in joined


def test_rewrite_meta_refuses_an_internally_inconsistent_recipe(updater, recipe):
    """Declares a platform but has no sha256 line for it -> must fail loudly.

    Note what is NOT an error any more: dropping *both* the platform and sha256
    lines for an arch is simply an older recipe shape, which the tool now adapts
    to. The error case is the recipe contradicting itself.
    """
    text = "\n".join(
        l for l in recipe.read_text().splitlines()
        if not ("sha256" in l and l.rstrip().endswith("# [win and arm64]"))
    )
    recipe.write_text(text)
    with pytest.raises(SystemExit) as e:
        updater.rewrite_meta(recipe, "10.0.302", "10.0.10", HASHES, True)
    assert "win and arm64" in str(e.value)


def test_dropping_a_platform_entirely_is_not_an_error(updater, recipe):
    """Both lines gone = an older shape, which must just work."""
    text = "\n".join(
        l for l in recipe.read_text().splitlines() if "# [win and arm64]" not in l
    )
    recipe.write_text(text)
    plats = updater.discover_platforms(recipe.read_text())
    assert len(plats) == 5
    hashes = {sel: f"{i + 1:064x}" for i, (sel, _r, _e) in enumerate(plats)}
    updater.rewrite_meta(recipe, "10.0.302", "10.0.10", hashes, True)
    assert 'set sdk_version = "10.0.302"' in recipe.read_text()


def test_rewrite_meta_refuses_a_recipe_missing_a_version_var(updater, recipe):
    recipe.write_text(recipe.read_text().replace("set runtime_version", "set rt_version"))
    with pytest.raises(SystemExit) as e:
        updater.rewrite_meta(recipe, "10.0.302", "10.0.10", HASHES, True)
    assert "runtime_version" in str(e.value)


# --------------------------------------------------------------------------
# render_block
# --------------------------------------------------------------------------


def test_render_block_covers_every_platform(updater):
    block = updater.render_block("10.0.302", "10.0.10", HASHES, updater.PLATFORMS)
    assert 'set sdk_version = "10.0.302"' in block
    assert 'set runtime_version = "10.0.10"' in block
    for selector, digest in HASHES.items():
        assert f"# [{selector}]" in block
        assert digest in block
    # One line per platform plus the three leading set/derive lines.
    assert len(block.splitlines()) == 3 + len(updater.PLATFORMS)


# --------------------------------------------------------------------------
# release selection
# --------------------------------------------------------------------------


def _release(sdk, runtime, rids=("linux-x64",), extra_sdks=()):
    files = []
    for rid in rids:
        ext = ".zip" if rid.startswith("win") else ".tar.gz"
        files.append(
            {
                "name": f"dotnet-sdk-{rid}{ext}",
                "rid": rid,
                "url": f"https://example/dotnet-sdk-{sdk}-{rid}{ext}",
                "hash": "f" * 128,
            }
        )
        # Installers share the rid; find_artifact must not pick these.
        inst = ".exe" if rid.startswith("win") else ".pkg"
        files.append({"name": f"dotnet-sdk-{rid}{inst}", "rid": rid, "url": "x", "hash": "y"})
    return {
        "release-version": runtime,
        "release-date": "2026-07-14",
        "runtime": {"version": runtime},
        "sdk": {"version": sdk, "files": files},
        "sdks": [{"version": s} for s in extra_sdks],
    }


def test_pick_release_defaults_to_newest(updater):
    rels = [_release("10.0.302", "10.0.10"), _release("10.0.301", "10.0.9")]
    assert updater.pick_release(rels, None)["sdk"]["version"] == "10.0.302"


def test_pick_release_finds_an_exact_sdk(updater):
    rels = [_release("10.0.302", "10.0.10"), _release("10.0.203", "10.0.7")]
    assert updater.pick_release(rels, "10.0.203")["runtime"]["version"] == "10.0.7"


def test_pick_release_searches_secondary_feature_bands(updater):
    # A release can ship several SDK bands; the wanted one may not be primary.
    rels = [_release("10.0.302", "10.0.10", extra_sdks=("10.0.203",))]
    assert updater.pick_release(rels, "10.0.203") is rels[0]


def test_pick_release_rejects_an_unknown_sdk(updater):
    with pytest.raises(SystemExit) as e:
        updater.pick_release([_release("10.0.302", "10.0.10")], "10.0.999")
    assert "10.0.999" in str(e.value)


def test_find_artifact_prefers_the_archive_over_the_installer(updater):
    sdk = _release("10.0.302", "10.0.10", rids=("win-x64",))["sdk"]
    got = updater.find_artifact(sdk, "win-x64", ".zip")
    assert got["name"].endswith(".zip")


def test_find_artifact_reports_available_rids_when_missing(updater):
    sdk = _release("10.0.302", "10.0.10", rids=("linux-x64",))["sdk"]
    with pytest.raises(SystemExit) as e:
        updater.find_artifact(sdk, "win-arm64", ".zip")
    msg = str(e.value)
    assert "win-arm64" in msg and "linux-x64" in msg


# --------------------------------------------------------------------------
# hashing: the sha512 cross-check is what makes the emitted sha256 trustworthy
# --------------------------------------------------------------------------


def test_stream_hashes_aborts_on_a_sha512_mismatch(updater, monkeypatch):
    payload = b"some bytes"
    monkeypatch.setattr(updater.urllib.request, "urlopen", _fake_urlopen(payload))
    with pytest.raises(SystemExit) as e:
        updater.stream_hashes("https://example/x", "0" * 128)
    assert "SHA-512 mismatch" in str(e.value)


def test_stream_hashes_returns_sha256_when_sha512_matches(updater, monkeypatch):
    import hashlib

    payload = b"some bytes"
    monkeypatch.setattr(updater.urllib.request, "urlopen", _fake_urlopen(payload))
    got = updater.stream_hashes("https://example/x", hashlib.sha512(payload).hexdigest())
    assert got == hashlib.sha256(payload).hexdigest()


def test_stream_hashes_detects_a_truncated_download(updater, monkeypatch):
    payload = b"short"
    monkeypatch.setattr(
        updater.urllib.request, "urlopen", _fake_urlopen(payload, declared_length=999)
    )
    with pytest.raises(SystemExit) as e:
        updater.stream_hashes("https://example/x", None)
    assert "short read" in str(e.value)


def _fake_urlopen(payload: bytes, declared_length: int | None = None):
    class Resp:
        headers = {"Content-Length": str(declared_length or len(payload))}

        def __init__(self):
            self._buf = payload

        def read(self, n=-1):
            out, self._buf = self._buf[:n], self._buf[n:]
            return out

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return lambda *a, **k: Resp()


# --------------------------------------------------------------------------
# shape adaptation
#
# The regression these guard against: PLATFORMS was a single global list matching
# the newest recipe, so bumping a branch cut before win-arm64 failed outright
# with "could not find the sha256 line for selector `# [win and x86_64]`". The
# tool was 100% broken for v8 and v9 while every test passed.
# --------------------------------------------------------------------------


def test_discover_platforms_reads_the_new_shape(updater, recipe):
    got = updater.discover_platforms(recipe.read_text())
    assert [(s, r) for s, r, _e in got] == [
        ("linux and aarch64", "linux-arm64"),
        ("linux and x86_64", "linux-x64"),
        ("osx and arm64", "osx-arm64"),
        ("osx and x86_64", "osx-x64"),
        ("win and x86_64", "win-x64"),
        ("win and arm64", "win-arm64"),
    ]


def test_discover_platforms_reads_the_old_bare_win_shape(updater, recipe_old_shape):
    got = updater.discover_platforms(recipe_old_shape.read_text())
    assert len(got) == 5
    assert ("win", "win-x64", ".zip") in got
    assert not any(sel == "win and arm64" for sel, _r, _e in got)


def test_extension_follows_the_rid(updater, recipe):
    for sel, rid, ext in updater.discover_platforms(recipe.read_text()):
        assert ext == (".zip" if rid.startswith("win") else ".tar.gz")


def test_discover_platforms_returns_none_without_platform_lines(updater):
    assert updater.discover_platforms("package:\n  name: x\n") is None


def test_platforms_for_falls_back_when_the_recipe_is_absent(updater, tmp_path):
    assert updater.platforms_for(tmp_path / "nope.yaml") == updater.PLATFORMS


def test_old_shape_recipe_can_be_written(updater, recipe_old_shape):
    """The exact case that was broken for v8 and v9."""
    plats = updater.discover_platforms(recipe_old_shape.read_text())
    hashes = {sel: f"{i + 1:064x}" for i, (sel, _r, _e) in enumerate(plats)}
    changes = updater.rewrite_meta(recipe_old_shape, "8.0.423", "8.0.29", hashes, True)
    text = recipe_old_shape.read_text()
    assert 'set sdk_version = "8.0.423"' in text
    # The bare selector must be preserved, not rewritten to the new form.
    assert "# [win]" in text
    assert "# [win and x86_64]" not in text
    assert any("sha256 [win]" in c for c in changes)


def test_old_shape_hash_lands_on_the_bare_win_selector(updater, recipe_old_shape):
    plats = updater.discover_platforms(recipe_old_shape.read_text())
    hashes = {sel: f"{i + 1:064x}" for i, (sel, _r, _e) in enumerate(plats)}
    updater.rewrite_meta(recipe_old_shape, "8.0.423", "8.0.29", hashes, True)
    line = next(
        l for l in recipe_old_shape.read_text().splitlines()
        if "sha256" in l and l.rstrip().endswith("# [win]")
    )
    assert hashes["win"] in line


def test_render_block_follows_the_given_shape(updater, recipe_old_shape):
    plats = updater.discover_platforms(recipe_old_shape.read_text())
    hashes = {sel: f"{i + 1:064x}" for i, (sel, _r, _e) in enumerate(plats)}
    block = updater.render_block("8.0.423", "8.0.29", hashes, plats)
    assert "# [win]" in block
    assert "# [win and arm64]" not in block
    assert len(block.splitlines()) == 3 + 5


# --------------------------------------------------------------------------
# validation: shell-safety and packageability are DIFFERENT questions
#
# Conflating them broke `--dry-run --channel 11.0` outright: a preview version's
# hyphen is unpackageable but perfectly safe to inspect, and rejecting it at
# resolve time destroyed the ability to look at a preview at all.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "11.0.100-preview.6.26359.118",  # hyphen: unpackageable, but SAFE to read
        "10.0.302",
        "8.0.423",
    ],
)
def test_shell_safe_accepts_plausible_versions_including_previews(updater, value):
    updater.assert_shell_safe("sdk version", value)  # must not raise


@pytest.mark.parametrize(
    "value",
    [
        "10.0.3; rm -rf /",
        "10.0.3$(whoami)",
        "10.0.3`id`",
        "10.0.3 && curl evil",
        "10.0.3|tee",
        "10.0.3\nnewline",
        "10.0.3'quote",
    ],
)
def test_shell_safe_rejects_dangerous_versions(updater, value):
    with pytest.raises(SystemExit) as e:
        updater.assert_shell_safe("sdk version", value)
    assert "refusing" in str(e.value)


def test_conda_version_problem_has_exactly_one_definition(updater, plan):
    """plan.py used to carry its own copy; two copies drift."""
    assert not hasattr(plan, "conda_version_problem"), (
        "plan.py should use updater.conda_version_problem, not redefine it"
    )
    assert updater.conda_version_problem("11.0.100-preview.6") is not None
    assert updater.conda_version_problem("10.0.302") is None


def test_channel_pattern(updater):
    for good in ("10.0", "3.1", "8.0"):
        assert updater.CHANNEL_RE.match(good)
    for bad in ("10", "10.0.302", "10.0-preview", "; rm -rf /", ""):
        assert not updater.CHANNEL_RE.match(bad)


# --------------------------------------------------------------------------
# recipe self-consistency
# --------------------------------------------------------------------------


def test_orphan_sha256_is_rejected(updater, recipe):
    """A sha256 line with no matching platform line must not be silently skipped.

    discover_platforms trusts the `platform` lines, so an orphan hash would be
    left STALE -- the recipe would then pair one platform's URL with another
    platform's hash and fail at download with a confusing mismatch, far from the
    cause.
    """
    text = "\n".join(
        l for l in recipe.read_text().splitlines()
        if not (l.startswith('{% set platform = "win-arm64"'))
    )
    recipe.write_text(text)
    plats = updater.discover_platforms(recipe.read_text())
    hashes = {sel: f"{i + 1:064x}" for i, (sel, _r, _e) in enumerate(plats)}
    with pytest.raises(SystemExit) as e:
        updater.rewrite_meta(recipe, "10.0.302", "10.0.10", hashes, True)
    msg = str(e.value)
    assert "win and arm64" in msg and "contradicts itself" in msg


def test_a_consistent_recipe_is_not_flagged(updater, recipe_old_shape):
    """The old five-platform shape is symmetric and must pass cleanly."""
    plats = updater.discover_platforms(recipe_old_shape.read_text())
    hashes = {sel: f"{i + 1:064x}" for i, (sel, _r, _e) in enumerate(plats)}
    updater.rewrite_meta(recipe_old_shape, "8.0.423", "8.0.29", hashes, True)
    assert 'set sdk_version = "8.0.423"' in recipe_old_shape.read_text()


# --------------------------------------------------------------------------
# platforms_for: a missing recipe is fine, an unreadable one is not
#
# Falling back on an unreadable recipe would silently apply the newest shape's
# selectors to a file we could not inspect, surfacing later as a wrong hash on
# some platform rather than as a read error here.
# --------------------------------------------------------------------------


def test_missing_recipe_falls_back_to_the_default_shape(updater, tmp_path):
    assert updater.platforms_for(tmp_path / "nope.yaml") == updater.PLATFORMS


def test_undecodable_recipe_is_fatal_not_a_silent_fallback(updater, tmp_path):
    """UnicodeDecodeError is a ValueError, so it was previously uncaught entirely."""
    bad = tmp_path / "meta.yaml"
    bad.write_bytes(b"\xff\xfe\x00binary garbage\x00\xff")
    with pytest.raises(SystemExit) as e:
        updater.platforms_for(bad)
    msg = str(e.value)
    assert "could not be read" in msg and "refusing to guess" in msg


def test_unreadable_recipe_is_fatal(updater, tmp_path):
    import os

    if os.geteuid() == 0:
        pytest.skip("root bypasses permission bits")
    p = tmp_path / "meta.yaml"
    p.write_text("{% set platform = \"linux-x64\" %}  # [linux and x86_64]\n")
    p.chmod(0o000)
    try:
        with pytest.raises(SystemExit) as e:
            updater.platforms_for(p)
        assert "could not be read" in str(e.value)
    finally:
        p.chmod(0o644)


def test_a_recipe_with_no_platform_lines_falls_back(updater, tmp_path):
    """Distinct from unreadable: readable but shapeless -> default is reasonable."""
    p = tmp_path / "meta.yaml"
    p.write_text("package:\n  name: x\n")
    assert updater.platforms_for(p) == updater.PLATFORMS
