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
    / "cloud_mcp_gateway_stack.py"
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


def test_ddb3_is_suppressed_against_the_spec_and_l1_is_fixed() -> None:
    """DDB3 defers to spec 006 R3.1/R3.2/R3.4; L1 is FIXED, not suppressed.

    Both findings once conflicted with spec 006, and both were suppressed rather
    than fixed because fixing either would have silently repealed a documented
    requirement -- a Category C change needing the spec amended first. On
    2026-08-07 the spec was amended and the two diverged:

    * **DDB3 stays suppressed.** R3.4 now records that PITR-off rests on the data
      being reconstructible, so the suppression is a contract decision with a
      written reason, not convenience. The guard keeps the reason citing the
      requirement and keeps PITR off, so a future edit cannot quietly enable it.
    * **L1 is no longer suppressed.** R4.2 became a property ("a currently-supported
      runtime") with the version pinned in code, so the rule passes unaided and
      remains available as the end-of-support alarm. Re-suppressing it would put
      the expiry back, which is the whole thing the amendment removed.

    The runtime assertion is deliberately *not* a literal version: R4.2 no longer
    names one, so pinning `PYTHON_3_14` here would re-encode the mistake the
    amendment fixed. It asserts only that the runtime is not the retired 3.11.
    """
    source = STACK_PY.read_text()

    # R4.2: a runtime is pinned in code, and it is not the retired one.
    assert "runtime=aws_lambda.Runtime.PYTHON_3_" in source, "R4.2 pins a runtime in code"
    assert "PYTHON_3_11" not in source, (
        "python3.11 was retired by the 2026-08-07 R4.2 amendment"
    )

    # R3.1/R3.2: PITR stays off.
    assert "point_in_time_recovery_enabled=True" not in source, "R3.1/R3.2 require PITR off"

    # DDB3 suppressed, citing the requirements it defers to.
    assert '"id": "AwsSolutions-DDB3"' in source
    assert "R3.1" in source, "the DDB3 reason must cite the requirement it defers to"
    assert "R3.4" in source, "the DDB3 reason must cite the rationale requirement"

    # L1 fixed, not suppressed.
    assert '"id": "AwsSolutions-L1"' not in source, (
        "L1 must be fixed by keeping the runtime current, not suppressed"
    )

