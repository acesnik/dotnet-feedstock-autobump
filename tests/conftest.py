"""Shared fixtures. Nothing here touches the network or a real feedstock.

Both scripts reach out to Microsoft's metadata, conda-forge's repodata, and git.
All three are single functions, so tests monkeypatch them rather than mocking at
the HTTP layer. Anything that would make a real request in a default test run is a
bug -- see test_live.py for the deliberately opt-in exceptions.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def updater():
    # Hyphenated filename, so it cannot be imported normally.
    return _load("updater", SCRIPTS / "update-dotnet-version.py")


@pytest.fixture(scope="session")
def plan():
    return _load("plan", SCRIPTS / "plan.py")


@pytest.fixture(scope="session")
def real_config():
    """The repo's actual channels.json, comments stripped.

    Tests assert against the shipped config on purpose: a change to `rid_map` or
    `active_subdirs` that breaks the audit should fail the suite.
    """
    mod = _load("plan_cfg", SCRIPTS / "plan.py")
    return mod.strip_comments(json.loads((ROOT / "channels.json").read_text()))


# A meta.yaml with the exact shape the updater's regexes depend on: two version
# variables, and per-platform sha256 lines distinguished only by their trailing
# selector comment. That last detail is the whole reason rewrite_meta needs
# testing -- the five hashes are otherwise identical in structure.
RECIPE = """\
{% set sdk_version = "10.0.100" %}
{% set runtime_version = "10.0.0" %}
{% set framework = '.'.join(sdk_version.split('.')[:2]) %}
{% set sha256 = "aaaa000000000000000000000000000000000000000000000000000000000000" %}  # [linux and aarch64]
{% set sha256 = "bbbb000000000000000000000000000000000000000000000000000000000000" %}  # [linux and x86_64]
{% set sha256 = "cccc000000000000000000000000000000000000000000000000000000000000" %}  # [osx and arm64]
{% set sha256 = "dddd000000000000000000000000000000000000000000000000000000000000" %}  # [osx and x86_64]
{% set sha256 = "eeee000000000000000000000000000000000000000000000000000000000000" %}  # [win and x86_64]
{% set sha256 = "ffff000000000000000000000000000000000000000000000000000000000000" %}  # [win and arm64]
{% set platform = "linux-arm64" %}  # [linux and aarch64]
{% set platform = "linux-x64" %}  # [linux and x86_64]
{% set platform = "osx-arm64" %}  # [osx and arm64]
{% set platform = "osx-x64" %}  # [osx and x86_64]
{% set platform = "win-x64" %}  # [win and x86_64]
{% set platform = "win-arm64" %}  # [win and arm64]

package:
  name: dotnet
  version: {{ sdk_version }}

build:
  number: 3
"""


# The pre-win-arm64 shape: five platforms, and a BARE `# [win]` rather than
# `# [win and x86_64]`. This is what upstream v8 and v9 actually carry, and the
# absence of such a fixture is why 54 tests passed while the updater was 100%
# broken for its stated purpose -- every other fixture used the newest shape.
RECIPE_OLD_SHAPE = """\
{% set sdk_version = "8.0.408" %}
{% set runtime_version = "8.0.15" %}
{% set framework = '.'.join(sdk_version.split('.')[:2]) %}
{% set sha256 = "aaaa000000000000000000000000000000000000000000000000000000000000" %}  # [linux and aarch64]
{% set sha256 = "bbbb000000000000000000000000000000000000000000000000000000000000" %}  # [linux and x86_64]
{% set sha256 = "cccc000000000000000000000000000000000000000000000000000000000000" %}  # [osx and arm64]
{% set sha256 = "dddd000000000000000000000000000000000000000000000000000000000000" %}  # [osx and x86_64]
{% set sha256 = "eeee000000000000000000000000000000000000000000000000000000000000" %}  # [win]
{% set platform = "linux-arm64" %}  # [linux and aarch64]
{% set platform = "linux-x64" %}  # [linux and x86_64]
{% set platform = "osx-arm64" %}  # [osx and arm64]
{% set platform = "osx-x64" %}  # [osx and x86_64]
{% set platform = "win-x64" %}  # [win]
{% set ext = "tar.gz" %}  # [not win]
{% set ext = "zip" %}  # [win]

package:
  name: dotnet
  version: {{ sdk_version }}

build:
  number: 1
"""


@pytest.fixture
def recipe_old_shape(tmp_path: Path) -> Path:
    d = tmp_path / "old" / "recipe"
    d.mkdir(parents=True)
    f = d / "meta.yaml"
    f.write_text(RECIPE_OLD_SHAPE)
    return f


@pytest.fixture
def recipe(tmp_path: Path) -> Path:
    d = tmp_path / "recipe"
    d.mkdir()
    f = d / "meta.yaml"
    f.write_text(RECIPE)
    return f


# Session-scoped: building a real git repo per test cost ~10s across the suite,
# and every test only reads from it via `git show`.
@pytest.fixture(scope="session")
def feedstock(tmp_path_factory):
    """A throwaway git repo with a branch per line, mimicking the real layout.

    plan.py reads recipes with `git show <ref>:recipe/meta.yaml`, so a real repo
    with real refs exercises that path rather than stubbing it out.
    """
    repo = tmp_path_factory.mktemp("fs_root") / "fs"
    (repo / "recipe").mkdir(parents=True)

    def run(*args):
        subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
        )

    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    run("symbolic-ref", "HEAD", "refs/heads/main")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")

    def commit_recipe(branch: str, sdk: str, runtime: str):
        run("checkout", "-q", "-B", branch)
        (repo / "recipe" / "meta.yaml").write_text(
            RECIPE.replace('"10.0.100"', f'"{sdk}"').replace('"10.0.0"', f'"{runtime}"')
        )
        run("add", "recipe/meta.yaml")
        run("commit", "-q", "-m", f"{branch}: {sdk}")

    commit_recipe("main", "10.0.100", "10.0.0")
    commit_recipe("v9", "9.0.203", "9.0.4")
    commit_recipe("v8", "8.0.407", "8.0.14")
    run("checkout", "-q", "main")
    return repo


def channel(ch, phase="active", rtype="lts", sdk=None, eol=None):
    """Terse channel-entry builder; tests are table-driven over these."""
    return {
        "channel": ch,
        "support_phase": phase,
        "release_type": rtype,
        "latest_sdk": sdk if sdk is not None else f"{ch}.100",
        "eol_date": eol,
    }


@pytest.fixture
def ch():
    return channel


@pytest.fixture(autouse=True)
def _no_network(monkeypatch, request):
    """Fail loudly if a default-run test tries to make a real request.

    Silent network access in a unit suite makes tests slow and flaky, and hides
    which behaviour is actually being asserted. Opt out with @pytest.mark.live.
    """
    if "live" in request.keywords:
        return
    import urllib.request

    def boom(*a, **k):  # pragma: no cover - only fires on a test bug
        raise AssertionError(
            "network access in an offline test; monkeypatch fetch_json / "
            "repodata_size, or mark the test @pytest.mark.live"
        )

    monkeypatch.setattr(urllib.request, "urlopen", boom)
