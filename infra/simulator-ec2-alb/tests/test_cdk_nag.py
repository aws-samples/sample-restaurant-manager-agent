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
    / "simulator_ec2_alb_stack.py"
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


def test_network_and_auth_hardening_is_fixed_not_suppressed() -> None:
    """VPC7 / EC28 / COG1 were FIXED here and must not regress into suppressions.

    These three are the perimeter of a stack whose ALB is internal: flow logs are
    the only record of what talked to what, and the Cognito password policy is
    the whole authentication bar for the dashboard.
    """
    source = STACK_PY.read_text()
    assert "flow_logs=" in source
    assert "detailed_monitoring=True" in source
    assert "password_policy=cognito.PasswordPolicy" in source
    for rule in ("AwsSolutions-VPC7", "AwsSolutions-EC28", "AwsSolutions-COG1"):
        assert f'"id": "{rule}"' not in source, f"{rule} must be fixed, not suppressed"
