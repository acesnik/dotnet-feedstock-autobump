"""ABI probe and check: does the recipe declare what the artifact requires?

Nothing here touches the network. The ELF parser is exercised against a real
(if minimal) ELF64 object built by `elf64_with_verneed` rather than a recorded
blob, so a change to the parser fails on structure rather than on a checksum.

The live counterparts -- asserting that Microsoft's actual 10.0 runtime really
does require 2.27 -- are in test_live.py, because a mocked artifact cannot catch
Microsoft changing one.
"""

from __future__ import annotations

import struct

import pytest


# --------------------------------------------------------------------------
# A minimal but structurally valid ELF64 object carrying a .gnu.version_r
# --------------------------------------------------------------------------
SHT_GNU_VERNEED = 0x6FFFFFFE


def elf64_with_verneed(needs: dict[str, list[str]], *, elf_class: int = 2) -> bytes:
    """Build an ELF64/LE object requiring `{library: [version, ...]}`.

    Deliberately hand-built: it exercises the linked-list walk (vn_next /
    vna_next) and the sh_link indirection to the string table, which are the two
    places a verneed parser goes wrong.
    """
    # String table: a leading NUL, then every name at a recorded offset.
    strings = bytearray(b"\x00")
    offsets: dict[str, int] = {}

    def intern(s: str) -> int:
        if s not in offsets:
            offsets[s] = len(strings)
            strings.extend(s.encode() + b"\x00")
        return offsets[s]

    for lib, versions in needs.items():
        intern(lib)
        for v in versions:
            intern(v)

    # Verneed entries, chained by vn_next; each with vn_cnt vernaux children
    # chained by vna_next, at offsets relative to the owning verneed entry.
    verneed = bytearray()
    libs = list(needs.items())
    for i, (lib, versions) in enumerate(libs):
        entry = bytearray()
        vn_next = 0 if i == len(libs) - 1 else 16 + 16 * len(versions)
        entry += struct.pack("<HHIII", 1, len(versions), intern(lib), 16, vn_next)
        for j, v in enumerate(versions):
            vna_next = 0 if j == len(versions) - 1 else 16
            entry += struct.pack("<IHHII", 0, 0, j + 2, intern(v), vna_next)
        verneed += entry

    ehsize, shentsize, shnum = 64, 64, 3
    shoff = ehsize
    str_off = shoff + shentsize * shnum
    vn_off = str_off + len(strings)

    hdr = bytearray(64)
    hdr[0:4] = b"\x7fELF"
    hdr[4] = elf_class  # EI_CLASS
    hdr[5] = 1  # EI_DATA: little-endian
    hdr[6] = 1  # EI_VERSION
    struct.pack_into("<HH", hdr, 0x10, 3, 0x3E)  # ET_DYN, x86-64
    struct.pack_into("<I", hdr, 0x14, 1)
    struct.pack_into("<Q", hdr, 0x28, shoff)
    struct.pack_into("<HH", hdr, 0x34, ehsize, 0)
    struct.pack_into("<HHH", hdr, 0x3A, shentsize, shnum, 0)

    def shdr(sh_type, sh_offset, sh_size, sh_link=0):
        b = bytearray(64)
        struct.pack_into("<I", b, 4, sh_type)
        struct.pack_into("<QQ", b, 0x18, sh_offset, sh_size)
        struct.pack_into("<I", b, 0x28, sh_link)
        return bytes(b)

    sections = (
        shdr(0, 0, 0)
        + shdr(3, str_off, len(strings))  # SHT_STRTAB
        + shdr(SHT_GNU_VERNEED, vn_off, len(verneed), sh_link=1)
    )
    return bytes(hdr) + sections + bytes(strings) + bytes(verneed)


# The exact soname sets observed in Microsoft's shipped crypto shims.
SONAMES_NET8 = {
    "libssl.so.1.0.0", "libssl.so.1.0.2", "libssl.so.1.1",
    "libssl.so.10", "libssl.so.11", "libssl.so.111", "libssl.so.3",
}
SONAMES_NET10 = {
    "libssl.so.1.0.0", "libssl.so.1.0.2", "libssl.so.1.1",
    "libssl.so.10", "libssl.so.3",
}
SONAMES_NET11 = {"libssl.so.1.1", "libssl.so.3", "libssl.so.4"}


# --------------------------------------------------------------------------
# ELF parsing
# --------------------------------------------------------------------------
def test_verneed_single_library(abi_probe):
    blob = elf64_with_verneed({"libc.so.6": ["GLIBC_2.14", "GLIBC_2.27", "GLIBC_2.4"]})
    assert abi_probe.glibc_versions(blob) == {"2.14", "2.27", "2.4"}
    assert abi_probe.max_version(abi_probe.glibc_versions(blob)) == "2.27"


def test_verneed_walks_the_whole_chain(abi_probe):
    """A parser that stops after the first verneed entry underreports the floor."""
    blob = elf64_with_verneed(
        {
            "libc.so.6": ["GLIBC_2.17"],
            "libm.so.6": ["GLIBC_2.27", "GLIBC_2.2.5"],
            "libdl.so.2": ["GLIBC_2.34"],
        }
    )
    assert abi_probe.glibc_versions(blob) == {"2.17", "2.27", "2.2.5", "2.34"}
    assert abi_probe.max_version(abi_probe.glibc_versions(blob)) == "2.34"


def test_non_glibc_versions_are_ignored(abi_probe):
    blob = elf64_with_verneed(
        {"libstdc++.so.6": ["GLIBCXX_3.4.20", "CXXABI_1.3"], "libc.so.6": ["GLIBC_2.17"]}
    )
    assert abi_probe.glibc_versions(blob) == {"2.17"}


@pytest.mark.parametrize("junk", [b"", b"not an elf", b"\x7fELF" + b"\x00" * 8])
def test_garbage_yields_nothing_rather_than_crashing(abi_probe, junk):
    assert abi_probe.glibc_versions(junk) == set()


def test_elf32_is_skipped_not_misparsed(abi_probe):
    """A 32-bit object must yield nothing, so the caller reports "unknown".

    Silently misparsing it would produce a confident wrong floor, which is worse
    than no answer -- the whole point of this check is that a wrong number went
    unnoticed for a release line.
    """
    blob = elf64_with_verneed({"libc.so.6": ["GLIBC_2.27"]}, elf_class=1)
    assert abi_probe.glibc_versions(blob) == set()


def test_version_ordering_is_numeric_not_lexical(abi_probe):
    # "2.9" > "2.27" lexically; the whole check inverts if this is wrong.
    assert abi_probe.max_version({"2.9", "2.27"}) == "2.27"
    assert abi_probe.max_version({"2.17", "2.4"}) == "2.17"
    assert abi_probe.max_version(set()) is None


# --------------------------------------------------------------------------
# openssl sonames -- the RedHat alias trap
# --------------------------------------------------------------------------
def test_soname_extraction(abi_probe):
    blob = b"\x00garbage\x00libssl.so.3\x00libcrypto.so.3\x00libssl.so.1.1\x00"
    assert abi_probe.openssl_sonames(blob) >= {
        "libssl.so.3", "libcrypto.so.3", "libssl.so.1.1"
    }


def test_redhat_aliases_are_not_read_as_majors(abi_probe):
    """.NET 8 lists libssl.so.10/.11/.111 -- RedHat aliases for 1.0.x/1.1.x.

    A numeric maximum over the soname list reports .NET 8 as supporting "openssl
    111". Every comparison against a real pin is then meaningless, and in the
    permissive direction: `openssl <4` would look satisfiable when it is the very
    pin that exists to stop openssl 4 being selected.
    """
    assert abi_probe.supported_openssl_majors(SONAMES_NET8) == ["1.1", "3"]
    naive = max(
        int(s.rsplit(".", 1)[1]) for s in SONAMES_NET8 if s.startswith("libssl.so.")
        and s.rsplit(".", 1)[1].isdigit()
    )
    assert naive == 111, "fixture no longer contains the alias this guards against"


@pytest.mark.parametrize(
    "sonames,expected",
    [
        (SONAMES_NET8, ["1.1", "3"]),
        (SONAMES_NET10, ["1.1", "3"]),
        (SONAMES_NET11, ["1.1", "3", "4"]),
        (set(), []),
        ({"libssl.so.1.1"}, ["1.1"]),
    ],
)
def test_supported_majors(abi_probe, sonames, expected):
    assert abi_probe.supported_openssl_majors(sonames) == expected


def test_probe_members_picks_the_shim_by_basename(abi_probe):
    members = {
        "shared/Microsoft.NETCore.App/10.0.10/libcoreclr.so": elf64_with_verneed(
            {"libc.so.6": ["GLIBC_2.27"]}
        ),
        "shared/Microsoft.NETCore.App/10.0.10/libclrgc.so": elf64_with_verneed(
            {"libc.so.6": ["GLIBC_2.17"]}
        ),
        "shared/Microsoft.NETCore.App/10.0.10/"
        "libSystem.Security.Cryptography.Native.OpenSsl.so": b"\x00libssl.so.3\x00",
    }
    out = abi_probe.probe_members(members)
    assert out["glibc_floor"] == "2.27"
    assert out["glibc_driver"] == [
        "shared/Microsoft.NETCore.App/10.0.10/libcoreclr.so"
    ]
    assert out["openssl_shim_found"] is True
    assert out["openssl_majors"] == ["3"]


def test_probe_members_without_a_shim_is_reported_not_assumed(abi_probe):
    out = abi_probe.probe_members(
        {"x.so": elf64_with_verneed({"libc.so.6": ["GLIBC_2.17"]})}
    )
    assert out["openssl_shim_found"] is False
    assert out["openssl_majors"] == []


# --------------------------------------------------------------------------
# Version constraints
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "constraint,version,expected",
    [
        ("<4", "3", True),
        ("<4", "4", False),
        ("<4", "1.1", True),
        ("", "4", True),          # no pin admits everything
        (">=3,<4", "3", True),
        (">=3,<4", "1.1", False),
        (">=3,<4", "4", False),
        ("=3", "3", True),
        ("=3", "4", False),
        ("3.*", "3", True),
        ("3.*", "4", False),
        ("!=4", "4", False),
        ("!=4", "3", True),
        ("<=3", "3", True),
        (">4", "4", False),
    ],
)
def test_permits(abi_check, abi_probe, constraint, version, expected):
    assert abi_check.permits(constraint, version, abi_probe.vkey) is expected


def test_unparseable_constraint_returns_none(abi_check, abi_probe):
    """None, not False. False would read as "excluded" and invent a finding."""
    assert abi_check.permits("~=3.0", "3", abi_probe.vkey) is None
    assert abi_check.permits("weird stuff", "3", abi_probe.vkey) is None


# --------------------------------------------------------------------------
# Reading what the recipe declares
# --------------------------------------------------------------------------
CBC = """\
c_stdlib_version:
  # Needed for CryptoKit
  - "11.3"  # [osx and x86_64]
  - "2.28"  # [linux]
"""

CBC_OSX_ONLY = """\
c_stdlib_version:
  # Needed for CryptoKit
  - "11.3"  # [osx and x86_64]
"""


@pytest.mark.parametrize("rid,expected", [("linux-x64", "2.28"), ("linux-arm64", "2.28")])
def test_declared_glibc(abi_check, rid, expected):
    assert abi_check.declared_glibc(CBC, rid) == expected


def test_declared_glibc_absent_for_linux(abi_check):
    """The pre-fix shape: only osx declares anything, so linux gets None."""
    assert abi_check.declared_glibc(CBC_OSX_ONLY, "linux-x64") is None
    assert abi_check.declared_glibc(None, "linux-x64") is None


def test_rendered_glibc_from_ci_support(abi_check):
    assert abi_check.rendered_glibc("c_stdlib_version:\n- '2.17'\n") == "2.17"
    assert abi_check.rendered_glibc("c_stdlib:\n- sysroot\n") is None


META_OPENSSL = """\
  - name: dotnet-runtime
    requirements:
      run:
        - icu  # [unix]
        - openssl <4  # [aarch64]
"""


def test_openssl_pin_applies_only_where_the_selector_says(abi_check):
    """The real recipe pins openssl on aarch64 ONLY.

    Pinning the selector semantics against the shipped shape: if `# [aarch64]`
    were read as matching linux-x64, the x64 exposure this check exists to find
    would be reported as already handled.
    """
    arm = abi_check.declared_openssl(META_OPENSSL, "linux-arm64")
    assert [(c, s) for c, s, _ok in arm] == [("<4", "aarch64")]
    assert abi_check.declared_openssl(META_OPENSSL, "linux-x64") == []


def test_unevaluable_selector_is_flagged_not_dropped(abi_check):
    meta = "        - openssl <4  # [nonsense_token and ???]\n"
    got = abi_check.declared_openssl(meta, "linux-x64")
    assert got and got[0][2] is None


# --------------------------------------------------------------------------
# The verdict
# --------------------------------------------------------------------------
def test_openssl_pin_matches_runtime(abi_check, abi_probe):
    v = abi_check.openssl_verdict(
        [("<4", "aarch64", True)], ["1.1", "3"], abi_probe.OPENSSL_SONAMES, abi_probe.vkey
    )
    assert v["admitted_unsupported"] == []
    assert v["supported_excluded"] == []


def test_undeclared_openssl_admits_an_unloadable_major(abi_check, abi_probe):
    """No pin is not protection: openssl 4 can still be in the environment."""
    v = abi_check.openssl_verdict([], ["1.1", "3"], abi_probe.OPENSSL_SONAMES, abi_probe.vkey)
    assert v["undeclared"] is True
    assert v["admitted_unsupported"] == ["4"]


def test_pin_becomes_over_restrictive_when_dotnet_adds_support(abi_check, abi_probe):
    """.NET 11 adds libssl.so.4, so `openssl <4` starts excluding a usable one."""
    v = abi_check.openssl_verdict(
        [("<4", None, True)], ["1.1", "3", "4"], abi_probe.OPENSSL_SONAMES, abi_probe.vkey
    )
    assert v["admitted_unsupported"] == []
    assert v["supported_excluded"] == ["4"]


def _result(channel="10.0", floor="2.27", declared="2.17", majors=("1.1", "3"),
            openssl=(("<4", "aarch64", True),), rid="linux-arm64", runtime="10.0.10"):
    return {
        "channel": channel,
        "branch": "main",
        "runtime_version": runtime,
        "rids": {
            rid: {
                "glibc_declared": declared,
                "glibc_rendered": declared,
                "openssl_declared": list(openssl),
                "probe": {
                    "glibc_floor": floor,
                    "glibc_driver": ["libclrjit.so"],
                    "glibc_inspected": 15,
                    "openssl_shim_found": True,
                    "openssl_sonames": ["libssl.so.3"],
                    "openssl_majors": list(majors),
                },
            }
        },
    }


def test_glibc_shortfall_becomes_an_issue(abi_check, abi_probe):
    issues, _notices = abi_check.findings(
        _result(floor="2.27", declared="2.17"),
        abi_probe.OPENSSL_SONAMES, abi_probe.vkey,
    )
    assert len(issues) == 1
    assert issues[0]["key"] == "glibc-floor-10.0-10.0.10"
    assert "2.27" in issues[0]["body"] and "2.17" in issues[0]["body"]


def test_matching_glibc_produces_no_issue(abi_check, abi_probe):
    issues, notices = abi_check.findings(
        _result(floor="2.17", declared="2.17"),
        abi_probe.OPENSSL_SONAMES, abi_probe.vkey,
    )
    assert issues == []
    assert notices == []


def test_declared_above_floor_is_fine(abi_check, abi_probe):
    """2.28 declared against a 2.27 floor is the intended fix, not a finding."""
    issues, notices = abi_check.findings(
        _result(floor="2.27", declared="2.28"),
        abi_probe.OPENSSL_SONAMES, abi_probe.vkey,
    )
    assert issues == []
    assert notices == []


def test_pending_rerender_is_a_notice_not_an_issue(abi_check, abi_probe):
    """conda_build_config fixed, .ci_support not yet regenerated.

    Filing an issue here would escalate a problem that is already fixed and
    merely awaiting a rerender.
    """
    r = _result(floor="2.27", declared="2.28")
    r["rids"]["linux-arm64"]["glibc_rendered"] = "2.17"
    issues, notices = abi_check.findings(r, abi_probe.OPENSSL_SONAMES, abi_probe.vkey)
    assert issues == []
    assert any("rerender" in n for n in notices)


def test_probe_failure_is_never_silent(abi_check, abi_probe):
    r = _result()
    r["rids"]["linux-arm64"] = {"probe": None, "probe_error": "URLError: timed out"}
    issues, notices = abi_check.findings(r, abi_probe.OPENSSL_SONAMES, abi_probe.vkey)
    assert issues == []
    assert any("could not probe" in n and "incomplete" in n for n in notices)


def test_openssl_exposure_becomes_an_issue(abi_check, abi_probe):
    issues, _ = abi_check.findings(
        _result(floor="2.17", declared="2.17", openssl=(), rid="linux-x64"),
        abi_probe.OPENSSL_SONAMES, abi_probe.vkey,
    )
    assert len(issues) == 1
    assert issues[0]["key"] == "openssl-soname-10.0-10.0.10"
    assert "undeclared" in issues[0]["body"]


def test_issue_keys_are_stable_for_dedup(abi_check, abi_probe):
    """The notify job dedups on `key`, so the same finding must key identically.

    An unstable key re-files the same issue every Monday.
    """
    a, _ = abi_check.findings(_result(), abi_probe.OPENSSL_SONAMES, abi_probe.vkey)
    b, _ = abi_check.findings(_result(), abi_probe.OPENSSL_SONAMES, abi_probe.vkey)
    assert [i["key"] for i in a] == [i["key"] for i in b]
    # ...and must change when the runtime does, so a new release is re-reported.
    c, _ = abi_check.findings(
        _result(runtime="10.0.11"), abi_probe.OPENSSL_SONAMES, abi_probe.vkey
    )
    assert [i["key"] for i in c] != [i["key"] for i in a]


def test_no_glibc_data_is_reported_as_suspect(abi_check, abi_probe):
    r = _result()
    r["rids"]["linux-arm64"]["probe"]["glibc_floor"] = None
    issues, notices = abi_check.findings(r, abi_probe.OPENSSL_SONAMES, abi_probe.vkey)
    assert issues == []
    assert any("suspect" in n for n in notices)


# --------------------------------------------------------------------------
# Config and plan integration
# --------------------------------------------------------------------------
def test_shipped_config_enables_the_check(real_config):
    """Assert against the real channels.json: disabling this by accident fails."""
    assert real_config.get("abi", {}).get("enabled") is True


def test_plan_carries_latest_runtime(plan, feedstock, real_config, ch):
    """abi_check builds a runtime tarball URL from this; without it, no probe."""
    channels = [dict(ch("10.0", sdk="10.0.302"), latest_runtime="10.0.10")]
    cfg = {"tracked": {"10.0": "main"}, "policy": "manual"}

    class FakeUpdater:
        @staticmethod
        def conda_version_problem(v):
            return None

    _bumps, _issues, lines, _notices, _tr = plan.plan_lines(
        cfg, channels, feedstock, FakeUpdater
    )
    assert lines[0]["latest_runtime"] == "10.0.10"


def test_ci_support_prefixes_cover_every_packaged_linux_rid(abi_check, updater):
    """A new Linux platform in the recipe must not be silently skipped."""
    linux = {rid for _s, rid, _e in updater.PLATFORMS if rid.startswith("linux-")}
    assert linux <= set(abi_check.CI_SUPPORT_PREFIX), (
        f"no .ci_support prefix mapped for {linux - set(abi_check.CI_SUPPORT_PREFIX)}"
    )


def test_unevaluable_glibc_selector_is_reported(abi_check):
    """A selector we cannot evaluate must not read as "nothing declared".

    Skipping it silently falls back to the rendered value, which is the same
    silent-wrong-number failure this module exists to catch.
    """
    cbc = 'c_stdlib_version:\n  - "2.28"  # [linux and ???]\n'
    problems = []
    assert abi_check.declared_glibc(cbc, "linux-x64", problems) is None
    assert problems == ["linux and ???"]


def test_unevaluable_openssl_selector_surfaces_as_a_notice(abi_check, abi_probe):
    r = _result(openssl=(("<4", "??bad??", None),))
    _issues, notices = abi_check.findings(r, abi_probe.OPENSSL_SONAMES, abi_probe.vkey)
    assert any("could not evaluate the selector" in n for n in notices)


def test_unevaluable_glibc_selector_surfaces_as_a_notice(abi_check, abi_probe):
    r = _result(floor="2.17", declared="2.17")
    r["rids"]["linux-arm64"]["glibc_selector_problems"] = ["linux and ???"]
    _issues, notices = abi_check.findings(r, abi_probe.OPENSSL_SONAMES, abi_probe.vkey)
    assert any("c_stdlib_version" in n and "may be wrong" in n for n in notices)


def test_openssl_issue_table_renders_an_empty_constraint(abi_check, abi_probe):
    """`- openssl` with no version must not render as a dangling backtick."""
    issues, _ = abi_check.findings(
        # glibc deliberately matching, so only the openssl issue is in play.
        _result(floor="2.17", declared="2.17", openssl=(("", None, True),),
                majors=("1.1", "3")),
        abi_probe.OPENSSL_SONAMES, abi_probe.vkey,
    )
    assert len(issues) == 1
    assert "`openssl`" in issues[0]["body"]
    assert "openssl `" not in issues[0]["body"]
