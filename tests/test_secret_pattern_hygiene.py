# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Guard: no tracked file carries a credential-SHAPED literal.

WHY THIS EXISTS
---------------
This regression has landed twice, in two different disguises:

1. ``tests/test_prompt_injection_adversarial.py`` used AWS's canonical example
   access key id as a fixture value. The value was never load-bearing --
   ``sanitize_result`` redacts on the *key name* -- but its shape made secret
   scanners raise a permanent ``aws-access-token`` finding.
2. The fix for (1) replaced the value, then added a comment explaining the
   replacement **that quoted the literal**. So the same rule fired again one
   line away, and the next scan reported it from the published snapshot.

(2) is the interesting failure: the remediation re-armed the very rule it was
closing, and no test noticed, because the offending text had moved from a string
into a comment. A scanner catches it only *after* the commit is already in
history -- and history is exactly what cannot be fixed afterwards. Both the
public snapshot and this repo are stuck with the original occurrence forever.

Hence a guard that runs before the commit, and that does not care whether the
text sits in a value, a comment, prose, or a doc.

HOW IT AVOIDS PUBLISHING WHAT IT PROTECTS AGAINST
-------------------------------------------------
The check is a *pattern*, never an instance: ``AKIA`` followed by 16
upper-alphanumerics. ``AKIA[0-9A-Z]{16}`` does not match itself, so this file
satisfies its own assertion. Asserting the property rather than denylisting a
known value is also strictly stronger -- it catches any credential-shaped
literal, not just the one example someone happened to paste.

SCOPE
-----
Tracked files only, via ``git ls-files``. Untracked and gitignored trees
(``.venv``, ``cdk.out``, vendored ``pip install -t .`` output) legitimately
contain example keys inside third-party SDK data, they are not ours, and they
are not what gets committed or published.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Shape of an AWS access key id: the AKIA prefix plus 16 more upper-alphanumerics.
# Built as a pattern on purpose -- see the module docstring. Never add a literal
# instance to this file.
_ACCESS_KEY_SHAPE = re.compile(r"AKIA[0-9A-Z]{16}")

# Binary / non-text suffixes we cannot meaningfully grep and would only slow the
# scan. Everything else is checked, including markdown, JSON, and YAML: the 2026-09-13
# recurrence was in a Python comment, and its siblings were in markdown prose.
_SKIP_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".woff", ".woff2", ".ttf", ".zip", ".gz"}
)


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [REPO_ROOT / p for p in out.split("\0") if p]


def test_no_tracked_file_contains_a_credential_shaped_literal() -> None:
    """No tracked file may contain an access-key-shaped literal, anywhere.

    Deliberately indifferent to context. A scanner is too, and so is git history.
    """
    offenders: list[str] = []

    for path in _tracked_files():
        if path.suffix.lower() in _SKIP_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable; nothing a secret scanner would parse either
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _ACCESS_KEY_SHAPE.search(line):
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{lineno}")

    assert not offenders, (
        "Credential-shaped literal(s) found in tracked files:\n  "
        + "\n  ".join(offenders)
        + "\n\nDescribe the shape instead of spelling an instance -- in values AND in "
        "comments and prose. Once committed, the finding is permanent: a secret scanner "
        "attributes it to the commit that added the line, and git history is not "
        "rewritten. Fix it before it lands, not after."
    )


def test_guard_detects_a_shaped_literal() -> None:
    """Mutation check: prove the matcher fires, so a pass above means something.

    Built by concatenation so no credential-shaped literal exists in this source
    -- otherwise this test would break the very invariant it verifies.
    """
    synthetic = "AKIA" + "A1B2C3D4E5F6G7H8"
    assert _ACCESS_KEY_SHAPE.search(synthetic) is not None

    # And that it is specific: the sanctioned placeholder must NOT match, since
    # hyphens and lowercase fall outside the shape.
    assert _ACCESS_KEY_SHAPE.search("AKIA-EXAMPLE-NOT-A-REAL-KEY") is None
