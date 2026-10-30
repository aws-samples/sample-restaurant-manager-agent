#!/usr/bin/env python3.12
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Regenerate every dependency lock file in the repo.

``pylock.toml`` (PEP 751) is the SOURCE OF TRUTH for dependency versions.
Everything else is derived:

    pyproject.toml / requirements*.txt   declare WHICH packages are needed
    pylock.toml                          pins WHICH VERSIONS, with hashes
    constraints.txt                      generated from pylock.toml, for pip

WHY A GENERATOR SCRIPT
----------------------
``uv pip compile`` preserves the versions already present in its output file
unless ``--upgrade`` is passed. That is what makes ``pylock.toml`` authoritative
rather than "whatever resolved today": re-running this script is idempotent, and
picking up new versions is an explicit, reviewable act (``--upgrade``, or
``--upgrade-package NAME``). Without that property a "refresh" would silently
drift the deployed version set -- which is how a fresh instance once resolved
mcp 2.0.0 and crash-looped on import (see constraints.txt).

Each lock is compiled for the environment that actually installs it, because
wheel selection is platform- and version-specific:

  * root project    -> Python 3.12 / aarch64-linux (the deployed EC2 simulator)
  * infra/*/lambdas -> that Lambda's runtime / aarch64-linux (arm64 Lambda)
  * infra/* (CDK)   -> Python 3.12 / x86_64-linux (the local dev host)

USAGE
-----
    scripts/lock_dependencies.py                  # idempotent regenerate
    scripts/lock_dependencies.py --upgrade        # pick up new versions
    scripts/lock_dependencies.py --upgrade-package boto3
    scripts/lock_dependencies.py --only infra/web-push
    scripts/lock_dependencies.py --check          # fail if anything is stale

IMPORTANT LIMITATION
--------------------
pip cannot install from ``pylock.toml``. The locks are consumed by dependency /
CVE scanners; the only lock wired into an install path is the root
``constraints.txt`` (``pip install -e . -c constraints.txt`` in the EC2
bootstrap). Lambda bundling still installs from ``requirements.txt``, so a
Lambda lock records versions without enforcing them. Wiring bundling to the
locks is deliberate future work -- see the finding referenced in the README.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import shutil
# bandit B404 false positive: subprocess is used only with a fixed argv and no shell; see the run() call below.
import subprocess  # nosec B404
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Local dev host (Amazon Linux 2023, x86_64) vs arm64 Lambda / arm64 EC2.
PLATFORM_LOCAL = "x86_64-manylinux2014"
PLATFORM_ARM = "aarch64-unknown-linux-gnu"

# Python version the *local dev venv* uses for CDK synth/deploy and tests.
CDK_PYTHON = "3.12"

# Lambda runtime per stack, read off the CDK stacks (lambda_.Runtime.PYTHON_3_*).
# Keep in lockstep with infra/<stack>/infrastructure/*.py.
LAMBDA_RUNTIME_BY_STACK: dict[str, str] = {
    "cloud-tool-relay": "3.14",
    "iot-core-broker": "3.14",
    "proactive-alerting": "3.14",
    "web-push": "3.14",
}

# Per-Lambda overrides where one stack mixes runtimes.
LAMBDA_RUNTIME_OVERRIDES: dict[str, str] = {}

# Marker in constraints.txt below which pins are machine-generated.
CONSTRAINTS_MARKER = (
    "# --- BEGIN GENERATED PINS - do not edit below this line ---"
)


# Directories never worth walking: CDK synth output and vendored trees are huge
# (asset copies of the whole repo) and contain no authored dependency sources.
PRUNE_DIRS = {"cdk.out", ".venv", "node_modules", "__pycache__", ".git", "dist"}


def iter_repo_files(root: Path, pattern: str) -> list[Path]:
    """Glob `pattern` under `root`, pruning PRUNE_DIRS during traversal.

    A plain ``rglob`` here costs seconds because it descends into every
    ``cdk.out/asset.*`` copy of the repo; pruning makes it milliseconds.
    """
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS]
        for name in fnmatch.filter(filenames, pattern):
            found.append(Path(dirpath) / name)
    return sorted(found)


def discover_lock_files() -> list[Path]:
    """Every committed lock file, root first. Shared with the test suite."""
    locks = [REPO_ROOT / "pylock.toml"]
    locks += iter_repo_files(REPO_ROOT / "infra", "pylock*.toml")
    return locks


# Directories whose lock must ALSO be projected to a sibling ``constraints.txt``.
#
# pip cannot install from a ``pylock.toml``, so any path that something actually
# pip-installs needs a constraints projection to make the lock *enforced* rather
# than merely recorded. Keyed by path relative to the repo root:
#
#   "."                          -> the EC2 bootstrap: pip install -e . -c constraints.txt
#   infra/web-push/lambdas/*     -> scripts/bundle.sh: pip install -r ... -c constraints.txt
#
# Every other Lambda uses ``Code.from_asset`` with **no** vendoring (it relies on
# the runtime's boto3), and the stack-level requirements install into dev venvs,
# so projecting those would create files nothing consumes.
#
# Add a path here when you add a bundler that pip-installs from it; a test
# asserts every bundle script that references ``constraints.txt`` has one.
PIP_INSTALLED_PATHS: frozenset[str] = frozenset(
    {
        ".",
        "infra/web-push/lambdas/push_sender",
        "infra/web-push/lambdas/rotator",
    }
)


@dataclass
class Target:
    """One lock file to generate."""

    source: Path  # pyproject.toml or requirements*.txt
    lock: Path  # pylock.toml / pylock.dev.toml
    python_version: str
    platform: str
    extra_args: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return str(self.source.relative_to(REPO_ROOT))

    @property
    def constraints(self) -> Path | None:
        """Sibling constraints.txt, when something pip-installs from this dir."""
        rel = self.source.parent.relative_to(REPO_ROOT).as_posix()
        if rel in PIP_INSTALLED_PATHS:
            return self.source.parent / "constraints.txt"
        return None


def _lambda_python_version(req: Path) -> str:
    rel = req.relative_to(REPO_ROOT).as_posix()
    for prefix, version in LAMBDA_RUNTIME_OVERRIDES.items():
        if rel.startswith(prefix + "/"):
            return version
    stack = req.relative_to(REPO_ROOT).parts[1]
    if stack not in LAMBDA_RUNTIME_BY_STACK:
        raise SystemExit(
            f"No Lambda runtime known for stack '{stack}' ({rel}). Add it to "
            "LAMBDA_RUNTIME_BY_STACK after checking the CDK stack's "
            "lambda_.Runtime.PYTHON_3_* value."
        )
    return LAMBDA_RUNTIME_BY_STACK[stack]


def discover_targets() -> list[Target]:
    targets = [
        Target(
            source=REPO_ROOT / "pyproject.toml",
            lock=REPO_ROOT / "pylock.toml",
            python_version=CDK_PYTHON,
            platform=PLATFORM_ARM,  # deployed EC2 is ARM64
            extra_args=["--all-extras"],
        )
    ]

    for req in iter_repo_files(REPO_ROOT / "infra", "requirements*.txt"):
        rel = req.relative_to(REPO_ROOT).as_posix()
        is_lambda = "/lambdas/" in rel
        # requirements-dev.txt -> pylock.dev.toml (PEP 751 allows pylock.<name>.toml)
        name = "pylock.dev.toml" if "-dev" in req.stem else "pylock.toml"
        targets.append(
            Target(
                source=req,
                lock=req.parent / name,
                python_version=(
                    _lambda_python_version(req) if is_lambda else CDK_PYTHON
                ),
                platform=PLATFORM_ARM if is_lambda else PLATFORM_LOCAL,
            )
        )
    return targets


def compile_lock(target: Target, upgrade_args: list[str], out: Path) -> None:
    """Run `uv pip compile` for one target, writing to `out`.

    Paths are passed **relative to the subprocess cwd**. ``uv`` records its own
    argv verbatim as a header comment in the generated lock, so passing absolute
    paths would bake the builder's home directory (and therefore their username)
    into every committed lock file. Comparisons elsewhere in this script parse
    package versions rather than raw text, so the header format is free to vary.
    """
    cwd = target.source.parent
    # Not a command-injection risk: this passes an argv LIST with no
    # ``shell=True`` (there is none anywhere in this repo), so no shell
    # interprets any element. Every non-literal part is in-script -- repo
    # relative paths, ``python_version`` from LAMBDA_RUNTIME_BY_STACK,
    # ``platform`` from module constants, and ``upgrade_args`` from this
    # script's own argparse flags. Nothing crosses a trust boundary. The argv is
    # spelled out inline so the full command is auditable at the call site.
    proc = subprocess.run(  # nosec B603, B607
        [
            "uv",
            "pip",
            "compile",
            os.path.relpath(target.source, cwd),
            "--python-version",
            target.python_version,
            "--python-platform",
            target.platform,
            "--format",
            "pylock.toml",
            "-o",
            os.path.relpath(out, cwd),
            "--quiet",
            *target.extra_args,
            *upgrade_args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise SystemExit(f"FAILED to compile lock for {target.label}")


def _empty_source(target: Target) -> bool:
    """True when a requirements file declares nothing (no lock is meaningful)."""
    if target.source.name == "pyproject.toml":
        return False
    for raw in target.source.read_text().splitlines():
        if raw.split("#", 1)[0].strip():
            return False
    return True


def locked_versions(lock: Path) -> dict[str, str]:
    data = tomllib.loads(lock.read_text())
    return {
        re.sub(r"[-_.]+", "-", p["name"]).lower(): p["version"]
        for p in data.get("packages", [])
        if "version" in p
    }


def constraint_pins(constraints: Path) -> dict[str, str]:
    """Parse ``name==version`` pins out of a constraints file."""
    pins: dict[str, str] = {}
    for raw in constraints.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if "==" not in line:
            continue
        name, version = line.split("==", 1)
        pins[re.sub(r"[-_.]+", "-", name).strip().lower()] = version.strip()
    return pins


#: Header written above the marker when creating a Lambda constraints projection.
#: The root ``constraints.txt`` has its own hand-maintained prose (it predates
#: this script and documents the mcp-2.0.0 outage), so it is never overwritten.
_LAMBDA_CONSTRAINTS_HEADER = """\
# GENERATED FILE - do not hand-edit the pins below the marker.
#
# Projection of {lock} for pip, created by scripts/lock_dependencies.py.
#
# WHY THIS FILE EXISTS
# --------------------
# scripts/bundle.sh vendors this Lambda's dependencies with
# `pip install -r requirements.txt -c constraints.txt -t .`. Without the `-c`
# the install resolves whatever is newest on the day of the bundle, so the
# deployed artifact's versions are luck rather than policy -- the same class of
# failure as the 2026-07-31 mcp-2.0.0 outage (see the root constraints.txt).
#
# It also makes dependency scanners agree with reality: they read the declared
# range in requirements.txt, so an old lower bound gets reported as if it were
# shipped even when the bundle resolves something current.
#
# pylock.toml is the SOURCE OF TRUTH. Regenerate both with:
#
#   scripts/lock_dependencies.py                          # keeps pins
#   scripts/lock_dependencies.py --upgrade-package NAME   # moves one package
#
# tests/test_dependency_lock.py fails if this file and the lock disagree.
"""


def render_constraints(
    pylock: Path, constraints: Path, *, create_header: bool = False
) -> None:
    """Regenerate the pin block of a constraints file from a pylock.

    Prose above CONSTRAINTS_MARKER is preserved so rationale stays editable;
    everything below is machine-written. When ``create_header`` is set and the
    file does not exist yet, a generated header is written first.
    """
    if not constraints.exists() and create_header:
        constraints.write_text(
            _LAMBDA_CONSTRAINTS_HEADER.format(
                lock=pylock.relative_to(REPO_ROOT).as_posix()
            )
            + CONSTRAINTS_MARKER
            + "\n"
        )

    text = constraints.read_text()
    if CONSTRAINTS_MARKER not in text:
        raise SystemExit(
            f"{constraints} is missing the generated-pins marker:\n"
            f"  {CONSTRAINTS_MARKER}\n"
            "Add it after the explanatory header, then re-run."
        )
    header = text.split(CONSTRAINTS_MARKER)[0].rstrip("\n")
    pins = "\n".join(
        f"{name}=={version}" for name, version in sorted(locked_versions(pylock).items())
    )
    constraints.write_text(f"{header}\n{CONSTRAINTS_MARKER}\n{pins}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--upgrade",
        action="store_true",
        help="allow all packages to move to newer versions (otherwise pins are kept)",
    )
    parser.add_argument(
        "--upgrade-package",
        action="append",
        default=[],
        metavar="NAME",
        help="allow only NAME to move (repeatable)",
    )
    parser.add_argument(
        "--only",
        default=None,
        metavar="PATH_PREFIX",
        help="restrict to targets whose source path starts with PATH_PREFIX",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit non-zero if any lock is missing or stale",
    )
    args = parser.parse_args()

    if not shutil.which("uv"):
        raise SystemExit("uv not found on PATH - see https://docs.astral.sh/uv/")

    upgrade_args = ["--upgrade"] if args.upgrade else []
    for pkg in args.upgrade_package:
        upgrade_args += ["--upgrade-package", pkg]
    if args.check and (args.upgrade or args.upgrade_package):
        raise SystemExit("--check cannot be combined with --upgrade/--upgrade-package")

    targets = discover_targets()
    if args.only:
        targets = [t for t in targets if t.label.startswith(args.only)]
        if not targets:
            raise SystemExit(f"--only {args.only!r} matched no targets")

    stale: list[str] = []
    skipped: list[str] = []
    for target in targets:
        if _empty_source(target):
            skipped.append(target.label)
            if target.lock.exists():
                target.lock.unlink()
            continue

        if args.check:
            if not target.lock.exists():
                stale.append(f"{target.label}: lock missing ({target.lock.name})")
                continue
            with tempfile.TemporaryDirectory() as tmp:
                probe = Path(tmp) / target.lock.name
                probe.write_text(target.lock.read_text())  # seed to preserve pins
                compile_lock(target, upgrade_args, probe)
                if locked_versions(probe) != locked_versions(target.lock):
                    stale.append(f"{target.label}: {target.lock.name} is stale")

            # A lock is only *enforced* if its constraints projection matches.
            expected = target.constraints
            if expected is not None:
                if not expected.exists():
                    stale.append(
                        f"{target.label}: constraints projection missing "
                        f"({expected.relative_to(REPO_ROOT)})"
                    )
                elif constraint_pins(expected) != locked_versions(target.lock):
                    stale.append(
                        f"{target.label}: {expected.relative_to(REPO_ROOT)} "
                        "disagrees with its lock"
                    )
        else:
            compile_lock(target, upgrade_args, target.lock)
            print(
                f"  locked {target.label} "
                f"(py{target.python_version}, {target.platform}) "
                f"-> {target.lock.relative_to(REPO_ROOT)}"
            )
            projection = target.constraints
            if projection is not None:
                render_constraints(target.lock, projection, create_header=True)
                print(
                    f"    projected -> {projection.relative_to(REPO_ROOT)} "
                    "(consumed by pip)"
                )

    if skipped:
        print(f"  skipped {len(skipped)} file(s) declaring no dependencies:")
        for label in skipped:
            print(f"    - {label}")

    if stale:
        print("\nSTALE LOCKS:", file=sys.stderr)
        for item in stale:
            print(f"  - {item}", file=sys.stderr)
        print(
            "\nRegenerate with: scripts/lock_dependencies.py", file=sys.stderr
        )
        return 1

    print(f"\nOK - {len(targets) - len(skipped)} lock file(s) up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
