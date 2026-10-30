# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""cdk-nag is applied and the stack synthesizes clean.

Before P1 this stack had **no** IaC security checking: ProtoShield's cdknag
plugin reported "No NagReport.csv files found" and the 5 "critical" findings in
the scan were scanner *errors*, not vulnerabilities. Applying
``AwsSolutionsChecks`` turns that into real signal -- and its first run found a
live least-privilege defect (a ``lambda:SendDurableExecutionCallback*`` wildcard
on ``function:durable-*``, functions deleted in spec 022 Task 6).

These tests guard the two things that can silently undo it: removing the Aspect
from ``app.py``, and suppressing a finding without recording why.
"""

from __future__ import annotations

from pathlib import Path

APP_PY = Path(__file__).resolve().parent.parent / "app.py"
STACK_PY = (
    Path(__file__).resolve().parent.parent
    / "infrastructure"
    / "web_push_stack.py"
)


def test_app_registers_aws_solutions_checks() -> None:
    """The Aspect must stay wired into the app.

    Without it, ``cdk synth`` stops failing on violations and no NagReport.csv is
    written, so both the deploy-time gate and the scanner's input disappear -- and
    nothing else would fail to signal that.
    """
    source = APP_PY.read_text()
    assert "from cdk_nag import AwsSolutionsChecks" in source
    assert "AwsSolutionsChecks(" in source
    assert "Aspects.of(app).add(" in source


def test_nag_reports_are_enabled() -> None:
    """``reports=True`` writes the CSV that the ProtoShield cdknag plugin reads."""
    assert "reports=True" in APP_PY.read_text()


def test_no_placeholder_suppression_reasons() -> None:
    """A suppression without evidence is worse than the finding it hides."""
    source = STACK_PY.read_text()
    for placeholder in ('"reason": "N/A"', '"reason": ""', '"reason": "TODO'):
        assert placeholder not in source, f"placeholder suppression reason: {placeholder}"


def test_pitr_is_enabled_rather_than_suppressed() -> None:
    """DDB3 was FIXED, not silenced - the table must keep PITR on.

    Losing the subscription table silently stops push alerts on every display
    until each one re-registers, which needs a human to re-grant notification
    permission. A future edit that drops PITR and suppresses DDB3 instead should
    fail here.
    """
    source = STACK_PY.read_text()
    assert "point_in_time_recovery_enabled=True" in source
    # Match a SUPPRESSION entry, not any mention: the rule id also appears in the
    # comment explaining why PITR was enabled, so a bare substring check fails.
    assert '"id": "AwsSolutions-DDB3"' not in source, (
        "DDB3 must be fixed (PITR enabled), not suppressed"
    )


def test_l1_is_fixed_rather_than_suppressed() -> None:
    """L1 was FIXED by moving the bundle image forward, not silenced.

    push_sender and rotator carried an L1 suppression for as long as
    ``scripts/bundle.sh`` built their wheels in a ``build-python3.12`` container,
    which pinned their runtime. The image now tracks the runtime, so all three
    Lambdas are on the latest and the rule passes unaided. Re-adding the suppression
    would let the runtime silently fall behind again -- which is exactly the drift
    the original suppression's reason string promised to come back and fix.
    """
    assert '"id": "AwsSolutions-L1"' not in STACK_PY.read_text(), (
        "L1 must be fixed (bundle image tracks the runtime), not suppressed"
    )
