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
def test_conda_version_problem(plan, version, ok):
    assert (plan.conda_version_problem(version) is None) is ok


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


def test_rewrite_meta_refuses_a_recipe_missing_a_selector(updater, recipe):
    # Drop the win-arm64 line: a recipe that has not been taught the platform
    # must fail loudly, not silently skip it.
    text = "\n".join(
        l for l in recipe.read_text().splitlines() if "# [win and arm64]" not in l
    )
    recipe.write_text(text)
    with pytest.raises(SystemExit) as e:
        updater.rewrite_meta(recipe, "10.0.302", "10.0.10", HASHES, True)
    assert "win and arm64" in str(e.value)


def test_rewrite_meta_refuses_a_recipe_missing_a_version_var(updater, recipe):
    recipe.write_text(recipe.read_text().replace("set runtime_version", "set rt_version"))
    with pytest.raises(SystemExit) as e:
        updater.rewrite_meta(recipe, "10.0.302", "10.0.10", HASHES, True)
    assert "runtime_version" in str(e.value)


# --------------------------------------------------------------------------
# render_block
# --------------------------------------------------------------------------


def test_render_block_covers_every_platform(updater):
    block = updater.render_block("10.0.302", "10.0.10", HASHES)
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
