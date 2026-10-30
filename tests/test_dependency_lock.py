# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Guards for the repo's dependency lock files.

AUTHORITY MODEL
---------------
``pylock.toml`` (PEP 751) is the SOURCE OF TRUTH for dependency versions.
Everything else is derived:

    pyproject.toml / requirements*.txt   declare WHICH packages are needed
    pylock.toml                          pins WHICH VERSIONS, with hashes
    constraints.txt                      generated projection of pylock.toml,
                                         because pip cannot install a pylock

Locks exist so dependency/CVE scanners can resolve the full transitive tree; a
ProtoShield scan reported scanning as incomplete ("NO LOCK FILE FOUND") when the
only pin record was ``constraints.txt``, which scanners do not recognize.

Regenerate everything with::

    scripts/lock_dependencies.py                  # idempotent; keeps pins
    scripts/lock_dependencies.py --upgrade        # move to newer versions
    scripts/lock_dependencies.py --check          # fail if anything is stale

WHAT THESE TESTS DO AND DO NOT COVER
------------------------------------
These are offline, structural checks: every declared dependency set has a lock,
every lock is valid PEP 751 with integrity hashes, and ``constraints.txt`` is an
exact projection of ``pylock.toml``. They deliberately do **not** re-resolve
against PyPI -- that needs the network and belongs in
``scripts/lock_dependencies.py --check``.

The consistency test between ``constraints.txt`` and ``pylock.toml`` is the
load-bearing one: those two files are coupled only by convention, and if they
drift, the lock describes versions nobody deploys while scanners report clean
results for the wrong artifacts.
"""

from __future__ import annotations

import functools
import importlib.util
import re
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PYLOCK = REPO_ROOT / "pylock.toml"
CONSTRAINTS = REPO_ROOT / "constraints.txt"
PYPROJECT = REPO_ROOT / "pyproject.toml"
GENERATOR = REPO_ROOT / "scripts" / "lock_dependencies.py"

CONSTRAINTS_MARKER = "# --- BEGIN GENERATED PINS - do not edit below this line ---"


def _load_generator():
    """Import scripts/lock_dependencies.py so target discovery has one owner."""
    if "lock_dependencies" in sys.modules:
        return sys.modules["lock_dependencies"]
    spec = importlib.util.spec_from_file_location("lock_dependencies", GENERATOR)
    assert spec and spec.loader, f"cannot import {GENERATOR}"
    module = importlib.util.module_from_spec(spec)
    # Register before exec: the module defines @dataclass types, and dataclasses
    # resolves string annotations via sys.modules[cls.__module__].
    sys.modules["lock_dependencies"] = module
    spec.loader.exec_module(module)
    return module


def _all_lock_files() -> list[Path]:
    """Reuse the generator's discovery so the two cannot disagree.

    It also prunes cdk.out/, which a plain rglob would descend into -- that walk
    costs seconds because each asset.* is a full copy of the repo.
    """
    return _load_generator().discover_lock_files()


def _normalize(name: str) -> str:
    """Normalize a distribution name per PEP 503."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _locked_versions(lock: Path) -> dict[str, str]:
    data = tomllib.loads(lock.read_text())
    return {
        _normalize(pkg["name"]): pkg["version"]
        for pkg in data.get("packages", [])
        if "version" in pkg
    }


def _constraint_pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw in CONSTRAINTS.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if "==" not in line:
            continue
        name, version = line.split("==", 1)
        pins[_normalize(name)] = version.strip()
    return pins


def _declared_direct_deps() -> set[str]:
    project = tomllib.loads(PYPROJECT.read_text())["project"]
    specs = list(project.get("dependencies", []))
    for extra_deps in project.get("optional-dependencies", {}).values():
        specs.extend(extra_deps)
    # Strip extras and specifiers: "uvicorn[standard]>=0.27.0" -> "uvicorn"
    return {_normalize(re.split(r"[\[><=!~;]", spec)[0]) for spec in specs}


ALL_LOCKS = _all_lock_files()
LOCK_IDS = [str(p.relative_to(REPO_ROOT)) for p in ALL_LOCKS]


# --------------------------------------------------------------------------
# Coverage: every declared dependency set must have a lock
# --------------------------------------------------------------------------


def test_root_lock_exists() -> None:
    assert PYLOCK.is_file(), (
        "pylock.toml is missing. Scanners cannot resolve the transitive tree "
        "without it. Regenerate with scripts/lock_dependencies.py"
    )


def test_generator_script_is_importable() -> None:
    """The generator owns the runtime/platform mapping; keep it loadable."""
    module = _load_generator()
    assert module.discover_targets(), "generator discovered no lock targets"


def test_every_dependency_source_has_a_lock() -> None:
    """Each pyproject/requirements file must ship a committed lock file.

    Sources that declare no dependencies are exempt -- a lock would be empty.
    """
    module = _load_generator()
    missing = []
    for target in module.discover_targets():
        if module._empty_source(target):
            continue
        if not target.lock.is_file():
            missing.append(
                f"{target.source.relative_to(REPO_ROOT)} -> "
                f"{target.lock.relative_to(REPO_ROOT)}"
            )
    assert not missing, (
        "dependency sources with no committed lock file: "
        f"{missing}. Run scripts/lock_dependencies.py"
    )


# --------------------------------------------------------------------------
# Structure: every lock is valid PEP 751 with integrity hashes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("lock", ALL_LOCKS, ids=LOCK_IDS)
def test_lock_is_valid_pep751(lock: Path) -> None:
    data = tomllib.loads(lock.read_text())
    assert data.get("lock-version") == "1.0", f"{lock}: unexpected lock-version"
    assert data.get("packages"), f"{lock}: declares no packages"
    assert data.get("requires-python"), f"{lock}: no requires-python recorded"


@pytest.mark.parametrize("lock", ALL_LOCKS, ids=LOCK_IDS)
def test_lock_packages_carry_integrity_hashes(lock: Path) -> None:
    """Every locked package needs a hashed artifact, or the lock proves nothing."""
    data = tomllib.loads(lock.read_text())
    unhashed = [
        pkg["name"]
        for pkg in data["packages"]
        if not pkg.get("sdist") and not pkg.get("wheels")
    ]
    assert not unhashed, f"{lock}: packages with no sdist or wheel: {unhashed}"


@pytest.mark.parametrize("lock", ALL_LOCKS, ids=LOCK_IDS)
def test_lock_pins_exact_versions(lock: Path) -> None:
    data = tomllib.loads(lock.read_text())
    unpinned = [pkg["name"] for pkg in data["packages"] if not pkg.get("version")]
    assert not unpinned, f"{lock}: packages without a pinned version: {unpinned}"


@pytest.mark.parametrize(
    "lock",
    [p for p in ALL_LOCKS if "/lambdas/" in p.as_posix()],
    ids=[s for s in LOCK_IDS if "/lambdas/" in s],
)
def test_lambda_locks_target_arm64(lock: Path) -> None:
    """Lambdas run arm64, so a lock must not carry x86_64 wheels.

    Bundling an x86_64 native wheel into an arm64 Lambda fails at cold start
    with `invalid ELF header` (see the project Lambda bundling notes).
    """
    text = lock.read_text()
    assert "x86_64" not in text and "amd64" not in text, (
        f"{lock}: contains x86_64/amd64 wheels but Lambdas are arm64. "
        "Regenerate with scripts/lock_dependencies.py"
    )


# --------------------------------------------------------------------------
# Enforcement: a lock only counts if the thing that installs actually uses it
# --------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _constraint_targets() -> tuple:
    """Targets whose lock must be projected to a sibling constraints.txt.

    Cached: pytest evaluates this at collection time for both the parametrize
    list and its ids, and target discovery walks the tree.
    """
    module = _load_generator()
    return tuple(t for t in module.discover_targets() if t.constraints is not None)


def test_pip_installed_paths_have_constraints_projections() -> None:
    """Every path something pip-installs from needs a constraints projection.

    pip cannot read a ``pylock.toml``, so without the projection the lock is
    recorded but not enforced and the install resolves whatever is newest that
    day.
    """
    missing = [
        str(t.constraints.relative_to(REPO_ROOT))
        for t in _constraint_targets()
        if not t.constraints.is_file()
    ]
    assert not missing, (
        f"constraints projections missing: {missing}. "
        "Run scripts/lock_dependencies.py"
    )


@pytest.mark.parametrize(
    "target",
    _constraint_targets(),
    ids=[str(t.constraints.relative_to(REPO_ROOT)) for t in _constraint_targets()],
)
def test_constraints_projection_matches_its_lock(target) -> None:
    """Each projection must be an exact projection of its own lock."""
    module = _load_generator()
    pins = module.constraint_pins(target.constraints)
    locked = _locked_versions(target.lock)
    mismatched = {
        name: (pinned, locked.get(name))
        for name, pinned in pins.items()
        if locked.get(name) != pinned
    }
    assert not mismatched, (
        f"{target.constraints.relative_to(REPO_ROOT)} disagrees with "
        f"{target.lock.relative_to(REPO_ROOT)}: {mismatched}"
    )
    assert set(locked) - set(pins) == set(), (
        "locked packages absent from the projection: "
        f"{sorted(set(locked) - set(pins))}"
    )


@functools.lru_cache(maxsize=1)
def _bundle_scripts() -> tuple[Path, ...]:
    """Every bundler, found via the generator's pruned walker.

    A plain rglob here descends into every cdk.out asset copy of the repo and
    costs seconds per call (see _all_lock_files).
    """
    module = _load_generator()
    return tuple(module.iter_repo_files(REPO_ROOT / "infra", "bundle.sh"))


@pytest.mark.parametrize(
    "script",
    _bundle_scripts(),
    ids=[str(p.relative_to(REPO_ROOT)) for p in _bundle_scripts()],
)
def test_bundle_scripts_install_with_constraints(script: Path) -> None:
    """A bundler must never pip-install without `-c constraints.txt`.

    Regression guard: `pip install -r requirements.txt -t . --upgrade` (the
    previous form) resolves newest-on-the-day, so the deployed artifact's
    versions were luck rather than policy. It is also why a CVE scanner reading
    the declared range disagreed with the shipped bundle.
    """
    text = script.read_text()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "pip install" not in stripped:
            continue
        assert "-c constraints.txt" in stripped or "-c ./constraints.txt" in stripped, (
            f"{script.relative_to(REPO_ROOT)} installs without constraints: "
            f"{stripped!r}"
        )



def test_constraints_is_marked_generated() -> None:
    text = CONSTRAINTS.read_text()
    assert CONSTRAINTS_MARKER in text, (
        "constraints.txt lost its generated-pins marker; "
        "scripts/lock_dependencies.py needs it to know what to rewrite"
    )
    assert "GENERATED FILE" in text.split(CONSTRAINTS_MARKER)[0]


def test_constraints_matches_pylock_exactly() -> None:
    """constraints.txt is what pip installs; pylock.toml is what scanners read.

    Any disagreement means the deployed versions and the scanned versions differ.
    """
    locked = _locked_versions(PYLOCK)
    pins = _constraint_pins()
    assert pins, "constraints.txt contained no '==' pins - parsing likely broke"

    mismatched = {
        name: (pinned, locked.get(name))
        for name, pinned in pins.items()
        if locked.get(name) != pinned
    }
    missing = sorted(set(locked) - set(pins))
    assert not mismatched, (
        "constraints.txt disagrees with pylock.toml (constraints, pylock): "
        f"{mismatched}. Run scripts/lock_dependencies.py"
    )
    assert not missing, (
        f"locked packages absent from constraints.txt: {missing}. "
        "Run scripts/lock_dependencies.py"
    )


def test_all_declared_dependencies_are_locked() -> None:
    """Every direct dependency, including every extra, must be resolved."""
    missing = sorted(_declared_direct_deps() - set(_locked_versions(PYLOCK)))
    assert not missing, (
        f"declared in pyproject.toml but absent from pylock.toml: {missing}. "
        "Regenerate with --all-extras so optional groups are covered."
    )


def test_root_lock_targets_the_deployed_python_floor() -> None:
    """The lock's floor must match pyproject's, or it locks the wrong tree.

    The voice extra's aws-sdk-bedrock-runtime requires >=3.12; a lower floor
    resolves the extra away and the deployed instance silently loses the voice
    SDK while still reporting healthy.
    """
    declared = tomllib.loads(PYPROJECT.read_text())["project"]["requires-python"]
    locked = tomllib.loads(PYLOCK.read_text())["requires-python"]
    assert declared == ">=3.12", f"unexpected requires-python in pyproject: {declared}"
    assert locked == declared, (
        f"pylock.toml requires-python ({locked}) != pyproject ({declared})"
    )
