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
    / "proactive_alerting_stack.py"
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


def test_state_machine_observability_is_fixed_not_suppressed() -> None:
    """SF1/SF2 were FIXED: this machine's decisions must stay auditable.

    Severity gating, quiet hours, the waitForTaskToken ack pause and the bounded
    re-surface loop all live in state transitions, so without ALL-event logging
    "why did this alert never re-surface?" is unanswerable after the fact.
    """
    source = STACK_PY.read_text()
    assert "level=sfn.LogLevel.ALL" in source
    assert "tracing_enabled=True" in source
    for rule in ("AwsSolutions-SF1", "AwsSolutions-SF2"):
        assert f'"id": "{rule}"' not in source, f"{rule} must be fixed, not suppressed"
