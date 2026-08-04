#!/usr/bin/env python3
"""Read ABI facts out of Microsoft's prebuilt runtime instead of assuming them.

Two facts, both coupled to the .NET version, both of which have already drifted
without anyone noticing:

* **glibc floor.** .NET 8 and 9 require glibc 2.17. .NET 10 raised it to 2.27.
  The recipe declared 2.17 throughout, so conda-forge published
  `dotnet-runtime 10.0.x` advertising `__glibc >=2.17,<3.0.a0` while the runtime
  cannot load below 2.27 -- it installs, then fails. A version bump touches only
  versions and hashes, so nothing in the existing tooling could have caught it.

* **openssl soname.** .NET's crypto shim `dlopen`s a hardcoded list of sonames.
  Through .NET 10 that list tops out at `libssl.so.3`, so openssl 4.x -- which
  ships only `libssl.so.4`, with no `.so.3` compatibility link -- breaks every
  crypto call, hence the `openssl <4` pin. .NET 11 preview.6 *adds*
  `libssl.so.4`, so that pin becomes wrong in the opposite direction the moment
  11 ships.

Both are read from the artifact rather than from a table in this repo, because a
table is precisely what went stale.

Usage (diagnostic; the checker imports the functions):

    abi_probe.py <runtime-tarball-url>
    abi_probe.py --rid linux-x64 --runtime 10.0.10
"""

from __future__ import annotations

import json
import re
import struct
import sys
import tarfile
import urllib.request

RUNTIME_URL = (
    "https://builds.dotnet.microsoft.com/dotnet/Runtime/"
    "{runtime}/dotnet-runtime-{runtime}-{rid}.tar.gz"
)
USER_AGENT = "dotnet-feedstock-autobump"

# The crypto shim is the only file whose openssl sonames matter.
CRYPTO_SHIM = "libSystem.Security.Cryptography.Native.OpenSsl.so"

# conda-forge openssl major -> the libssl soname that major provides.
#
# Mapping majors onto sonames, rather than parsing numbers out of the sonames,
# is deliberate. .NET 8's shim lists `libssl.so.10`, `libssl.so.11` and
# `libssl.so.111`, which are *RedHat* aliases for the 1.0.x and 1.1.x series --
# not majors 10, 11 and 111. Taking a numeric maximum over the soname list
# reports .NET 8 as supporting "openssl 111", which is nonsense, and would make
# every comparison against a real pin meaningless. What actually matters is the
# narrow question this dict asks: for each openssl the solver could pick, does
# the shim name the soname that openssl installs?
OPENSSL_SONAMES = {
    "1.1": "libssl.so.1.1",
    "3": "libssl.so.3",
    "4": "libssl.so.4",
}

# Sonames are string literals in .rodata, fed to dlopen.
SONAME_RE = re.compile(rb"lib(?:ssl|crypto)\.so(?:\.[0-9]+)*")

SHT_GNU_VERNEED = 0x6FFFFFFE


def _elf64_sections(data: bytes):
    """Yield (sh_type, sh_offset, sh_size, sh_link) for a 64-bit little-endian ELF.

    Only ELF64/LE is handled: every architecture this feedstock packages
    (linux-x64, linux-arm64) is ELF64 little-endian. A 32-bit or big-endian
    object yields nothing rather than being misparsed, and the caller turns that
    into a visible "could not determine" instead of a wrong number.
    """
    if len(data) < 64 or data[:4] != b"\x7fELF":
        return
    if data[4] != 2 or data[5] != 1:  # EI_CLASS != ELFCLASS64, or not LSB
        return
    e_shoff, = struct.unpack_from("<Q", data, 0x28)
    e_shentsize, e_shnum = struct.unpack_from("<HH", data, 0x3A)
    if e_shentsize < 64 or e_shoff == 0:
        return
    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        if off + 64 > len(data):
            return
        sh_type, = struct.unpack_from("<I", data, off + 4)
        sh_offset, sh_size = struct.unpack_from("<QQ", data, off + 0x18)
        sh_link, = struct.unpack_from("<I", data, off + 0x28)
        yield sh_type, sh_offset, sh_size, sh_link


def _cstr(data: bytes, start: int) -> str:
    end = data.find(b"\x00", start)
    return data[start : end if end != -1 else len(data)].decode("latin1")


def glibc_versions(data: bytes) -> set[str]:
    """Every GLIBC_x.y version this ELF object *requires*.

    Read from `.gnu.version_r` (SHT_GNU_verneed), which is the structure the
    linker writes to record "I need these symbol versions from these libraries".
    That is the same information `check-glibc` from cf-nvidia-tools derives by
    walking the symbol table -- verified to agree exactly on eight artifacts
    across .NET 8, 9, 10 and 11 -- but read from the authoritative structure
    rather than from strings, so a `GLIBC_` literal sitting in unrelated data
    cannot inflate the answer.
    """
    found: set[str] = set()
    sections = list(_elf64_sections(data))
    by_index = {i: s for i, s in enumerate(sections)}
    for sh_type, sh_offset, sh_size, sh_link in sections:
        if sh_type != SHT_GNU_VERNEED:
            continue
        strtab = by_index.get(sh_link)
        if strtab is None:
            continue
        _st_type, st_off, st_size, _st_link = strtab
        strings = data[st_off : st_off + st_size]
        pos = sh_offset
        # Verneed entries form a linked list via vn_next; each has vn_cnt
        # vernaux children linked via vna_next. Offsets are relative to the
        # entry that owns them, not to the section.
        while 0 < pos < sh_offset + sh_size and pos + 16 <= len(data):
            _vn_version, vn_cnt, _vn_file, vn_aux, vn_next = struct.unpack_from(
                "<HHIII", data, pos
            )
            aux = pos + vn_aux
            for _ in range(vn_cnt):
                if aux + 16 > len(data):
                    break
                _hash, _flags, _other, vna_name, vna_next = struct.unpack_from(
                    "<IHHII", data, aux
                )
                name = _cstr(strings, vna_name)
                if name.startswith("GLIBC_"):
                    found.add(name[len("GLIBC_") :])
                if vna_next == 0:
                    break
                aux += vna_next
            if vn_next == 0:
                break
            pos += vn_next
    return found


def vkey(v: str) -> tuple:
    """Sort key for a dotted version; unparseable components sort as 0."""
    return tuple(int(p) if p.isdigit() else 0 for p in str(v).split("."))


def max_version(versions) -> str | None:
    return max(versions, key=vkey) if versions else None


def openssl_sonames(data: bytes) -> set[str]:
    """libssl/libcrypto sonames named as dlopen literals in this object."""
    return {m.decode("latin1") for m in SONAME_RE.findall(data)}


def supported_openssl_majors(sonames) -> list[str]:
    """Which conda-forge openssl majors this shim can actually load.

    See OPENSSL_SONAMES for why this is a lookup and not arithmetic.
    """
    return [maj for maj, so in OPENSSL_SONAMES.items() if so in sonames]


def probe_members(members: dict[str, bytes]) -> dict:
    """The pure core: ABI facts from {member name -> bytes}.

    Separated from the download so it is testable without network or a 30 MB
    artifact, and so a caller that already has the bytes can reuse it.
    """
    per_file: dict[str, str] = {}
    for name, blob in members.items():
        top = max_version(glibc_versions(blob))
        if top:
            per_file[name] = top
    floor = max_version(per_file.values())
    driver = None
    if floor:
        driver = sorted(n for n, v in per_file.items() if v == floor)[:3]

    shim = next(
        (b for n, b in members.items() if n.rsplit("/", 1)[-1] == CRYPTO_SHIM), None
    )
    sonames = sorted(openssl_sonames(shim)) if shim is not None else []

    return {
        "glibc_floor": floor,
        "glibc_driver": driver,
        "glibc_inspected": len(members),
        "openssl_shim_found": shim is not None,
        "openssl_sonames": sonames,
        "openssl_majors": supported_openssl_majors(sonames),
    }


def _wanted(name: str) -> bool:
    """Native objects worth inspecting, from a runtime tarball's layout.

    The runtime tarball -- not the SDK's -- on purpose: it is ~30 MB against
    ~240 MB, and the native code that sets both facts lives entirely in it.
    Confirmed by extraction: the aspnetcore shared framework and the sdk tree
    ship zero `.so` files, so only `dotnet-runtime` constrains anything.
    """
    base = name.rsplit("/", 1)[-1]
    return name.endswith(".so") or base == "dotnet"


def probe_url(url: str, timeout: int = 600) -> dict:
    """Download and inspect a runtime tarball, streaming rather than saving it.

    tarfile's `r|gz` mode reads sequentially from the socket, so only the members
    being inspected are ever held in memory -- a few MB at a time out of ~30 MB
    transferred.
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    members: dict[str, bytes] = {}
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        with tarfile.open(fileobj=resp, mode="r|gz") as tar:
            for member in tar:
                if not member.isfile() or not _wanted(member.name):
                    continue
                f = tar.extractfile(member)
                if f is None:
                    continue
                members[member.name.lstrip("./")] = f.read()
    out = probe_members(members)
    out["url"] = url
    return out


def probe(rid: str, runtime_version: str, timeout: int = 600) -> dict:
    return probe_url(
        RUNTIME_URL.format(runtime=runtime_version, rid=rid), timeout=timeout
    )


def main(argv: list[str]) -> int:
    if len(argv) == 2 and not argv[1].startswith("-"):
        print(json.dumps(probe_url(argv[1]), indent=2))
        return 0
    if len(argv) == 5 and argv[1] == "--rid" and argv[3] == "--runtime":
        print(json.dumps(probe(argv[2], argv[4]), indent=2))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
