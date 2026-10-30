#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Vendor native Lambda deps for arm64 BEFORE `cdk deploy` (spec 025 Task 1d/1f;
# rotator added spec 028). The Lambdas are arm64 (lambda-steering); cryptography
# ships native .so files, so they must be built in an arm64 container, not
# installed from the x86 dev host. The CDK assets then use plain
# Code.from_asset (no synth-time bundling), so synth + the unit tests stay
# Docker-free.
#
# One-time per host boot (registers QEMU for arm64 emulation on an x86 host):
#   docker run --rm --privileged multiarch/qemu-user-static --reset -p yes
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

# The cpython ABI of the wheels installed below is the ABI of THIS image, and a
# Lambda can only load .so files built for its own runtime -- a mismatch fails at
# cold start with "invalid ELF header". So this version is not a free choice: it
# must equal the `runtime=lambda_.Runtime.PYTHON_3_*` of PushSenderFn/RotatorFn in
# infrastructure/web_push_stack.py, and the LAMBDA_RUNTIME_BY_STACK["web-push"]
# entry in scripts/lock_dependencies.py that the locks are compiled against.
# Moving the runtime forward means moving all three together, then re-running this
# script (checked by tests/test_stack.py::test_bundle_image_matches_lambda_runtime).
PY_VERSION="3.14"
BUILD_IMAGE="public.ecr.aws/sam/build-python${PY_VERSION}:latest"

bundle_one() {
  local lambda_dir="$1"
  cd "$ROOT/lambdas/$lambda_dir"
  # Purge previously-vendored artifacts BEFORE installing.
  #
  # `pip install -t . --upgrade` overwrites package code but does NOT remove the
  # superseded *.dist-info, so upgrading leaves BOTH versions' metadata behind
  # (observed 2026-08-05: cryptography-49.0.0.dist-info and 50.0.0.dist-info
  # side by side after the CVE-2026-69247 bump). The code that wins at runtime is
  # the new one, but any SBOM/CVE scanner reading the artifact still sees the old
  # version and reports a vulnerability that is not shipped -- exactly the
  # scanner-vs-reality gap the constraints below exist to close.
  #
  # Everything untracked in this directory is generated, so "delete what git does
  # not track" is the precise rule. The delete runs INSIDE the container as root
  # because the previous bundle's files are root-owned (pip ran as root there).
  local keep_expr=()
  while IFS= read -r tracked; do
    keep_expr+=(! -name "$tracked")
  done < <(git ls-files . | sed 's|.*/||' | sort -u)
  docker run --rm --platform linux/arm64 \
    -v "$(pwd):/var/task" -w /var/task \
    "$BUILD_IMAGE" \
    find . -mindepth 1 -maxdepth 1 "${keep_expr[@]}" -exec rm -rf {} +

  # `-c constraints.txt` pins every version, transitive ones included, to the set
  # in pylock.toml (constraints.txt is a generated projection of it -- regenerate
  # both with scripts/lock_dependencies.py). WITHOUT it, this install resolves
  # whatever is newest on the day of the bundle, so the deployed artifact's
  # versions are luck rather than policy -- the same class of failure as the
  # 2026-07-31 mcp-2.0.0 outage on the EC2 venv.
  #
  # `--upgrade` is kept ON PURPOSE alongside `-c`: with the constraints pinning
  # exact versions, --upgrade means "move to exactly the pinned version", which
  # is deterministic rather than "take the newest".
  if [ ! -f constraints.txt ]; then
    echo "ERROR: $lambda_dir/constraints.txt missing - run scripts/lock_dependencies.py" >&2
    exit 1
  fi
  docker run --rm --platform linux/arm64 \
    -v "$(pwd):/var/task" -w /var/task \
    "$BUILD_IMAGE" \
    pip install -r requirements.txt -c constraints.txt -t . --upgrade

  # Hand the vendored files back to the host user.
  #
  # pip ran as root inside the container, so without this every vendored file is
  # root:root on the host. That is invisible day to day (the files are
  # gitignored) but breaks anything that needs to UNLINK them:
  #   * `rm -rf` on the directory fails with Permission denied -- while `git rm`
  #     SUCCEEDS, because the tracked files are not the root-owned ones, so you
  #     end up with deletions staged against a directory still on disk;
  #   * `git checkout <branch>` / `merge` / `reset --hard` fail half-applied when
  #     they need to remove a previously-tracked file.
  # Both cost real time during the 2026-08-05 durable-layer removal (30,070
  # root-owned files in one package). Fixing it here means nobody needs the
  # after-the-fact `sudo find ... -exec chown` recovery.
  #
  # Done in-container because only root can chown these; `id -u`/`id -g` are
  # evaluated on the HOST, so they resolve to the invoking user, not to root.
  docker run --rm --platform linux/arm64 \
    -v "$(pwd):/var/task" -w /var/task \
    "$BUILD_IMAGE" \
    chown -R "$(id -u):$(id -g)" .
  # Sanity: fail if any x86_64 .so leaked in (would crash at Lambda cold start
  # with "invalid ELF header").
  if find . -name '*x86_64-linux-gnu.so' | grep -q .; then
    echo "ERROR: x86_64 .so in $lambda_dir (arch mismatch); re-run the QEMU registration" >&2
    exit 1
  fi
  # Sanity: every version-tagged .so is built for THIS cpython, so a bundle made
  # with a stale image cannot be shipped against a newer runtime. Extension
  # modules tagged `abi3` (cryptography) are version-independent by construction
  # and are correctly not matched by the version-specific glob below.
  local wrong_abi
  wrong_abi=$(find . -name '*.cpython-*.so' ! -name "*.cpython-${PY_VERSION/./}-*.so" | head -5)
  if [ -n "$wrong_abi" ]; then
    echo "ERROR: $lambda_dir has .so files for the wrong cpython ABI (want ${PY_VERSION}):" >&2
    echo "$wrong_abi" >&2
    exit 1
  fi
  # Sanity: exactly one dist-info per distribution, so a scanner cannot see a
  # stale version alongside the shipped one.
  local dupes
  dupes=$(find . -maxdepth 1 -name '*.dist-info' | sed 's|.*/||; s|-[0-9][^-]*\.dist-info$||' | sort | uniq -d)
  if [ -n "$dupes" ]; then
    echo "ERROR: multiple versions vendored in $lambda_dir: $dupes" >&2
    exit 1
  fi
  echo "$lambda_dir deps vendored for arm64 ($(find . -maxdepth 1 -name '*.dist-info' | wc -l) packages)"
}

bundle_one push_sender   # pywebpush -> cryptography/http-ece
bundle_one rotator       # cryptography (EC P-256 keygen)
