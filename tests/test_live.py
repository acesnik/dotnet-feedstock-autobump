"""Opt-in tests that hit the real upstream endpoints.

    pytest -m live

Excluded from the default run so the suite stays offline and fast. They exist
because the mocked tests cannot catch the single most likely real-world breakage:
**Microsoft or anaconda.org changing the shape of their JSON.** Every offline test
asserts against fixtures I wrote, so all of them would keep passing happily while
the scripts fell over in production.

These assert only the fields the scripts actually read, and nothing about values
that legitimately change -- no version numbers, no counts. A failure here means
an upstream contract moved, not that a release happened.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


def test_release_index_has_the_fields_we_read(updater):
    index = updater.fetch_json(updater.INDEX_URL)
    assert "releases-index" in index
    entries = index["releases-index"]
    assert entries, "release index is empty"
    for e in entries:
        for key in (
            "channel-version",
            "support-phase",
            "release-type",
            "latest-sdk",
            # plan.py carries this through so abi_check.py can build a runtime
            # tarball URL. Without it no ABI probe happens at all.
            "latest-runtime",
            "releases.json",
        ):
            assert key in e, f"{key} missing from a channel entry"


def test_support_phase_and_release_type_use_the_values_we_branch_on(updater):
    """plan.py compares these strings literally, so a renamed value is silent."""
    index = updater.fetch_json(updater.INDEX_URL)
    phases = {e["support-phase"] for e in index["releases-index"]}
    types = {e["release-type"] for e in index["releases-index"]}
    assert phases <= {"preview", "go-live", "active", "maintenance", "eol"}, phases
    assert types <= {"lts", "sts"}, types
    # The three we make decisions on must all still be represented somewhere.
    assert {"active", "eol"} <= phases


def test_channel_releases_have_the_sdk_runtime_pairing(updater):
    """The sdk/runtime pairing is the thing the generic autotick bot cannot do."""
    index = updater.fetch_json(updater.INDEX_URL)
    active = next(e for e in index["releases-index"] if e["support-phase"] == "active")
    releases = updater.fetch_json(active["releases.json"])["releases"]
    assert releases
    r = releases[0]
    assert "runtime" in r and "version" in r["runtime"]
    assert "sdk" in r and "version" in r["sdk"]
    assert "files" in r["sdk"] and r["sdk"]["files"]


def test_sdk_files_carry_rid_url_and_a_sha512(updater):
    index = updater.fetch_json(updater.INDEX_URL)
    active = next(e for e in index["releases-index"] if e["support-phase"] == "active")
    sdk = updater.fetch_json(active["releases.json"])["releases"][0]["sdk"]
    archives = [f for f in sdk["files"] if f["name"].endswith((".tar.gz", ".zip"))]
    assert archives, "no archive artifacts at all"
    for f in archives:
        assert {"name", "rid", "url", "hash"} <= set(f)
        # 128 hex chars: still SHA-512, which is why re-hashing to sha256 is
        # necessary at all. If this ever becomes 64, the script can be simplified.
        assert len(f["hash"]) == 128, f"{f['name']} hash is not sha512"


def test_every_platform_the_recipe_needs_is_still_published(updater):
    """A dropped RID breaks the next bump; the audit should catch it, but assert."""
    index = updater.fetch_json(updater.INDEX_URL)
    active = next(e for e in index["releases-index"] if e["support-phase"] == "active")
    sdk = updater.fetch_json(active["releases.json"])["releases"][0]["sdk"]
    for _selector, rid, ext in updater.PLATFORMS:
        # Raises SystemExit with a helpful message if absent.
        art = updater.find_artifact(sdk, rid, ext)
        assert art["url"].endswith(ext)


def test_repodata_head_returns_a_content_length(plan, real_config):
    """The viability heuristic depends entirely on this header existing."""
    for subdir in ("linux-64", "win-arm64"):
        size = plan.repodata_size(subdir)
        assert size is not None, f"no Content-Length for {subdir}"
        assert size > 0


def test_a_nonexistent_subdir_returns_none(plan):
    assert plan.repodata_size("linux-nonesuch") is None


def test_the_declared_active_subdirs_all_exist(plan, real_config):
    """Catches a typo in channels.json, and a subdir conda-forge retires."""
    for subdir in real_config["active_subdirs"]:
        assert plan.repodata_size(subdir) is not None, f"{subdir} has no repodata"


def test_runtime_tarball_url_pattern_still_resolves(abi_probe, updater):
    """abi_probe builds this URL by hand, so a renamed path breaks it silently.

    Nothing else in the repo fetches the *runtime* archive -- the updater hashes
    the SDK -- so if Microsoft moves this path, the ABI check degrades to a
    "could not probe" notice every week and nothing else complains.
    """
    import urllib.request

    index = updater.fetch_json(updater.INDEX_URL)
    active = next(e for e in index["releases-index"] if e["support-phase"] == "active")
    runtime = active["latest-runtime"]
    for rid in ("linux-x64", "linux-arm64"):
        url = abi_probe.RUNTIME_URL.format(runtime=runtime, rid=rid)
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=60) as resp:
            assert resp.status == 200, url
            assert int(resp.headers["Content-Length"]) > 0


def test_probing_a_real_runtime_yields_plausible_abi_facts(abi_probe, updater):
    """End-to-end against a real ~30 MB artifact.

    Asserts shape and plausibility, not specific numbers: the floor moving is a
    real event the bot files an issue about, so pinning `2.27` here would turn a
    correctly-detected upstream change into a red test suite. What must not
    happen is the parser silently returning nothing -- that would make every
    future drift invisible, which is the failure this check exists to prevent.
    """
    import re

    index = updater.fetch_json(updater.INDEX_URL)
    active = next(e for e in index["releases-index"] if e["support-phase"] == "active")
    out = abi_probe.probe("linux-x64", active["latest-runtime"])

    assert out["glibc_inspected"] > 5, "almost no native objects found in the tarball"
    assert out["glibc_floor"] is not None, "no GLIBC_ requirements parsed at all"
    assert re.fullmatch(r"2\.\d+(\.\d+)?", out["glibc_floor"]), out["glibc_floor"]

    assert out["openssl_shim_found"], "crypto shim missing or renamed"
    assert out["openssl_majors"], "no recognised openssl soname in the shim"
    # Every shipped .NET has named libssl.so.3 since 6.0; losing it would mean
    # the soname list was restructured and OPENSSL_SONAMES needs revisiting.
    assert "3" in out["openssl_majors"], out["openssl_sonames"]


def test_the_recipe_url_host_still_serves_the_artifacts(updater):
    """The recipe downloads from dotnetcli.azureedge.net, not the metadata host.

    Those are different hostnames fronting the same blobs. If the CDN in the
    recipe ever stops serving, every build breaks while the metadata looks fine.
    """
    import urllib.request

    index = updater.fetch_json(updater.INDEX_URL)
    active = next(e for e in index["releases-index"] if e["support-phase"] == "active")
    sdk = updater.fetch_json(active["releases.json"])["releases"][0]["sdk"]
    art = updater.find_artifact(sdk, "linux-x64", ".tar.gz")
    recipe_url = art["url"].replace(
        "builds.dotnet.microsoft.com", "dotnetcli.azureedge.net"
    )
    req = urllib.request.Request(recipe_url, method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as resp:
        assert resp.status == 200
        assert int(resp.headers["Content-Length"]) > 0
