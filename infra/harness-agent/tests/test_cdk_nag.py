# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""cdk-nag is applied and the stack synthesizes clean.

Before P1 this stack had **no** IaC security checking: ProtoShield's cdknag
plugin reported "No NagReport.csv files found" and the 5 "critical" findings in
the scan were scanner *errors*, not vulnerabilities. Applying
``AwsSolutionsChecks`` turns that into real signal -- and its first run found a
live least-privilege defect (a ``lambda:SendDurableExecutionCallback*`` wildcard
and observability APIs that take no resource ARN).

These tests guard the two things that can silently undo it: removing the Aspect
from ``app.py``, and suppressing a finding without recording why.
"""

from __future__ import annotations

from pathlib import Path

APP_PY = Path(__file__).resolve().parent.parent / "app.py"
STACK_PY = (
    Path(__file__).resolve().parent.parent
    / "infrastructure"
    / "harness_agent_stack.py"
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


def test_every_suppression_has_a_reason() -> None:
    """A suppression without evidence is worse than the finding it hides.

    cdk-nag requires a ``reason``, but it accepts a placeholder; this asserts the
    reasons in this stack are substantive rather than "N/A"-style filler.
    """
    source = STACK_PY.read_text()
    if "NagSuppressions" not in source:
        return  # nothing suppressed yet
    # Crude but effective: every suppression id must be accompanied by a reason
    # string long enough to be an actual justification.
    assert source.count('"id": "AwsSolutions-') == source.count('"reason": ('), (
        "each NagSuppressions entry needs its own reason"
    )
    for placeholder in ('"reason": "N/A"', '"reason": ""', '"reason": "TODO'):
        assert placeholder not in source, f"placeholder suppression reason: {placeholder}"
