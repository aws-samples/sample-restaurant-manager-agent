# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Guards the MIT-0 SPDX license header on first-party source files.

WHY
---
This repo publishes a curated snapshot to a public AWS sandbox, so every source
file that ships needs an accurate license header. ``LICENSE`` is **MIT-0** (MIT
No Attribution -- note the missing "the above copyright notice ... shall be
included" clause, which is what separates MIT-0 from MIT), the standard AWS
Guidance sample-code license.

The header is the canonical 2-line AWS form::

    Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
    SPDX-License-Identifier: MIT-0

WHY A TEST AND NOT JUST THE SCANNER
-----------------------------------
A ProtoShield scan reported 202 files missing a header -- but measured against
``Apache 2.0``, the scanner plugin's default, which this repo does not grant.
Two lessons are baked in here:

1. The finding was real (0% compliance) while its *prescription* was wrong.
   Adding Apache-2.0 headers would have cleared the finding by making every file
   claim the wrong license. So this test asserts the identifier matches
   ``LICENSE`` rather than hardcoding a license name -- change the project's
   license and this fails until the headers follow.
2. A scanner-only guard means the next 200 files drift before anyone re-runs the
   scan. This runs in the ordinary suite, offline, in well under a second.

WHAT IT DOES NOT COVER
----------------------
File types the publishing surface does not include (Markdown, YAML, JSON, HCL)
are out of scope, matching the scanner's extension list. Vendored dependencies
carry their own upstream licenses and are excluded -- rewriting a third party's
header would be a licensing error, not a fix.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Source extensions that must carry a header. Mirrors the ProtoShield
#: license-headers plugin's list, restricted to what this repo actually contains.
SOURCE_SUFFIXES = frozenset({".py", ".js", ".jsx", ".ts", ".tsx", ".sh"})

#: The SPDX identifier the headers must declare. Asserted against LICENSE by
#: test_identifier_matches_license_file rather than trusted on its own.
SPDX_IDENTIFIER = "MIT-0"

#: Required first line -- attribution. Without it a bare SPDX tag would pass.
COPYRIGHT_MARKER = "Copyright Amazon.com"

#: Bytes from the top of a file inspected for the header, matching the scanner.
HEADER_SCAN_BYTES = 8192

#: Path fragments whose files are third-party or generated. A vendored package
#: keeps its upstream header; a lock/asset directory is not source.
_EXCLUDED_FRAGMENTS = (
    "/node_modules/",
    "/.venv/",
    "/venv/",
    "/cdk.out/",
    "/dist/",
    "/build/",
    "/__pycache__/",
    "/site-packages/",
)


def _tracked_source_files() -> list[Path]:
    """Git-tracked source files, which is exactly the publishable surface.

    Using git rather than a filesystem walk means vendored dependencies (all
    gitignored) are excluded by construction rather than by an ignore list that
    has to be kept current.
    """
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    )
    files = []
    for rel in result.stdout.split("\0"):
        if not rel:
            continue
        if any(fragment in f"/{rel}" for fragment in _EXCLUDED_FRAGMENTS):
            continue
        path = REPO_ROOT / rel
        if path.suffix in SOURCE_SUFFIXES and path.is_file():
            files.append(path)
    return files


@pytest.fixture(scope="module")
def source_files() -> list[Path]:
    files = _tracked_source_files()
    # Guard the guard: if this ever returns nothing (a git failure, a suffix
    # typo), every assertion below would vacuously pass.
    assert len(files) > 100, (
        f"expected the repo to contain >100 tracked source files, found "
        f"{len(files)}. The collection step is broken, so the header checks "
        "below would pass without checking anything."
    )
    return files


def test_identifier_matches_license_file() -> None:
    """The header's SPDX id must describe the license the repo actually grants.

    MIT-0 is identified by what it OMITS -- MIT's requirement that the copyright
    notice be included in all copies. Asserting the grant text and the absence
    of that clause is what distinguishes it; a substring match on "MIT" would
    accept either license.
    """
    license_text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Permission is hereby granted, free of charge" in license_text, (
        "LICENSE is not an MIT-family license, but the source headers declare "
        f"SPDX-License-Identifier: {SPDX_IDENTIFIER}. Update LICENSE and the "
        "headers together -- LICENSE is the source of truth."
    )
    assert "notice shall be included" not in license_text, (
        "LICENSE contains MIT's attribution-retention clause, so it is MIT, not "
        f"MIT-0, and the headers' SPDX-License-Identifier: {SPDX_IDENTIFIER} is "
        "wrong. These must agree."
    )


def test_every_source_file_has_the_spdx_header(source_files: list[Path]) -> None:
    expected = f"SPDX-License-Identifier: {SPDX_IDENTIFIER}"
    missing = [
        str(p.relative_to(REPO_ROOT))
        for p in source_files
        if expected not in p.read_text(encoding="utf-8", errors="replace")[
            :HEADER_SCAN_BYTES
        ]
    ]
    assert not missing, (
        f"{len(missing)} source file(s) are missing '{expected}':\n  "
        + "\n  ".join(sorted(missing)[:25])
        + ("\n  ..." if len(missing) > 25 else "")
    )


def test_every_source_file_has_the_copyright_line(source_files: list[Path]) -> None:
    missing = [
        str(p.relative_to(REPO_ROOT))
        for p in source_files
        if COPYRIGHT_MARKER not in p.read_text(encoding="utf-8", errors="replace")[
            :HEADER_SCAN_BYTES
        ]
    ]
    assert not missing, (
        f"{len(missing)} source file(s) are missing the '{COPYRIGHT_MARKER}' "
        "attribution line:\n  " + "\n  ".join(sorted(missing)[:25])
        + ("\n  ..." if len(missing) > 25 else "")
    )


def test_shebang_stays_on_the_first_line(source_files: list[Path]) -> None:
    """A header inserted above a shebang silently breaks execution.

    The kernel and cloud-init both only honour ``#!`` on line 1, so an
    executable script or an EC2 user-data file whose header was prepended
    instead of inserted would still lint clean and still be a header-compliant
    file -- it just would not run. Nothing else in the suite would catch it.
    """
    offenders = []
    for path in source_files:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for index, line in enumerate(lines[:6]):
            if line.startswith("#!") and index != 0:
                offenders.append(f"{path.relative_to(REPO_ROOT)} (line {index + 1})")
                break
    assert not offenders, (
        "shebang must be on line 1; the license header goes immediately after "
        f"it: {offenders}"
    )
