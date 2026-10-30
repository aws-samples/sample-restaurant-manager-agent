# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for HarnessAgentStack (spec 022 Task 1).

Synthesizes the stack and asserts the execution role is least-privilege:
  - trust policy allows bedrock-agentcore.amazonaws.com
  - Bedrock model invoke + scoped AgentCore Memory present
  - memory + model statements are NOT Resource "*"
  - DELIBERATELY ABSENT: InvokeAgentRuntime, InvokeGateway, iam:*, Action "*"
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from infrastructure.harness_agent_stack import HarnessAgentStack

#: The context the stack is actually deployed with. Read from disk rather than
#: duplicated, so a test cannot pass against values no deploy would ever use.
_CDK_JSON_CONTEXT: dict = json.loads(
    (Path(__file__).resolve().parents[1] / "cdk.json").read_text(encoding="utf-8")
)["context"]

MEMORY_ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:memory/SellerAssistantMemory-EXAMPLE123"
MODEL_ID = "us.anthropic.claude-sonnet-4-20250514-v1:0"
ROLE_NAME = "restaurant-harness-agent-exec-role"
HARNESS_NAME = _CDK_JSON_CONTEXT["harnessName"]

_CONTEXT = {
    "memoryArn": MEMORY_ARN,
    "bedrockModelId": MODEL_ID,
    "executionRoleName": ROLE_NAME,
    "harnessName": HARNESS_NAME,
}

#: IAM actions this stack grants that accept NO resource ARN, per the AWS
#: service authorization reference. A statement may use Resource "*" only when
#: EVERY action in it is on this list -- see
#: test_resource_star_only_on_unscopable_actions. Adding an entry here is a
#: deliberate assertion that AWS provides no way to scope that action; it is not
#: a place to park an action that is merely inconvenient to scope.
_UNSCOPABLE_ACTIONS = frozenset(
    {
        "xray:PutTraceSegments",
        "xray:PutTelemetryRecords",
        "xray:GetSamplingRules",
        "xray:GetSamplingTargets",
        "cloudwatch:PutMetricData",
        "ecr-public:GetAuthorizationToken",
        "sts:GetServiceBearerToken",
    }
)


def _synth() -> Template:
    app = cdk.App(context=dict(_CONTEXT))
    stack = HarnessAgentStack(
        app,
        "HarnessAgentStack",
        env=cdk.Environment(account="123456789012", region="us-west-2"),
    )
    return Template.from_stack(stack)


def _all_statements(template: Template) -> list[dict]:
    """Collect every IAM policy statement (inline managed policies on the role)."""
    statements: list[dict] = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        doc = policy["Properties"]["PolicyDocument"]
        statements.extend(doc.get("Statement", []))
    return statements


def _actions(stmt: dict) -> list[str]:
    a = stmt.get("Action", [])
    return [a] if isinstance(a, str) else list(a)


# ---------------------------------------------------------------------------
# Region lock + context validation
# ---------------------------------------------------------------------------


def test_region_lock_rejects_non_us_west_2() -> None:
    app = cdk.App(context=dict(_CONTEXT))
    with pytest.raises(ValueError, match="us-west-2"):
        HarnessAgentStack(
            app,
            "HarnessAgentStack",
            env=cdk.Environment(account="123456789012", region="us-east-1"),
        )


def test_missing_memory_arn_raises() -> None:
    ctx = dict(_CONTEXT)
    del ctx["memoryArn"]
    app = cdk.App(context=ctx)
    with pytest.raises(ValueError, match="memoryArn"):
        HarnessAgentStack(
            app,
            "HarnessAgentStack",
            env=cdk.Environment(account="123456789012", region="us-west-2"),
        )


# ---------------------------------------------------------------------------
# Role shape
# ---------------------------------------------------------------------------


def test_exactly_one_role_trusting_agentcore() -> None:
    template = _synth()
    roles = template.find_resources("AWS::IAM::Role")
    assert len(roles) == 1, f"expected exactly one role, got {len(roles)}"
    role = next(iter(roles.values()))
    principals = [
        s["Principal"]["Service"]
        for s in role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    ]
    flat = []
    for p in principals:
        flat.extend(p if isinstance(p, list) else [p])
    assert "bedrock-agentcore.amazonaws.com" in flat


def test_role_name_is_deterministic() -> None:
    template = _synth()
    template.has_resource_properties("AWS::IAM::Role", {"RoleName": ROLE_NAME})


# ---------------------------------------------------------------------------
# Required grants present
# ---------------------------------------------------------------------------


def test_bedrock_model_invoke_present() -> None:
    statements = _all_statements(_synth())
    model_actions = {a for s in statements for a in _actions(s) if a.startswith("bedrock:")}
    assert "bedrock:InvokeModel" in model_actions
    assert "bedrock:InvokeModelWithResponseStream" in model_actions


def test_memory_data_plane_scoped_to_memory_arn() -> None:
    statements = _all_statements(_synth())
    mem_stmts = [
        s
        for s in statements
        if any(a == "bedrock-agentcore:CreateEvent" for a in _actions(s))
    ]
    assert mem_stmts, "no AgentCore Memory data-plane statement found"
    for s in mem_stmts:
        resources = s["Resource"]
        resources = [resources] if isinstance(resources, str) else resources
        # Every resource must reference the specific memory ARN, never "*".
        assert all(r != "*" for r in resources)
        assert any(
            (isinstance(r, str) and MEMORY_ARN in r) for r in resources
        ), f"memory statement not scoped to the memory ARN: {resources}"


# ---------------------------------------------------------------------------
# Least-privilege negative assertions (the security core of Task 1)
# ---------------------------------------------------------------------------


def test_no_invoke_agent_runtime() -> None:
    statements = _all_statements(_synth())
    all_actions = {a for s in statements for a in _actions(s)}
    assert not any("InvokeAgentRuntime" in a for a in all_actions), (
        "InvokeAgentRuntime (raw command exec) must not be granted (R5.4)"
    )


def test_no_invoke_gateway() -> None:
    statements = _all_statements(_synth())
    all_actions = {a for s in statements for a in _actions(s)}
    assert not any("InvokeGateway" in a for a in all_actions), (
        "InvokeGateway must not be granted on the inline tool path (OD1)"
    )
    # Also no broad bedrock-agentcore:* wildcard.
    assert "bedrock-agentcore:*" not in all_actions


def test_no_iam_or_admin_or_star_actions() -> None:
    statements = _all_statements(_synth())
    for s in statements:
        for a in _actions(s):
            assert a != "*", "wildcard Action '*' must not be granted"
            assert not a.startswith("iam:"), f"iam:* action must not be granted: {a}"
            assert not a.startswith("sts:AssumeRole"), f"AssumeRole must not be granted: {a}"


def test_model_and_memory_not_resource_star() -> None:
    """Bedrock-model and memory statements must be ARN-scoped, not '*'."""
    statements = _all_statements(_synth())
    for s in statements:
        acts = _actions(s)
        is_model = any(a.startswith("bedrock:") for a in acts)
        is_memory = any(a == "bedrock-agentcore:CreateEvent" for a in acts)
        if is_model or is_memory:
            resources = s["Resource"]
            resources = [resources] if isinstance(resources, str) else resources
            assert all(r != "*" for r in resources), (
                f"scoped statement {acts} must not use Resource '*'"
            )


# ---------------------------------------------------------------------------
# Observability scoping
# ---------------------------------------------------------------------------


def test_logs_actions_scoped_to_this_harness_log_groups() -> None:
    """CloudWatch Logs must be pinned to this harness's own log-group prefix.

    The runtime derives the log-group name from the harness name
    (``/aws/bedrock-agentcore/runtimes/harness_{harnessName}-{runtimeId}-DEFAULT``),
    so everything except the 10-character runtimeId is knowable at synth time.
    A ``Resource "*"`` here would let the harness write into -- and enumerate --
    every log group in the account, including other stacks' Lambda logs.
    """
    statements = _all_statements(_synth())
    logs_statements = [
        s for s in statements if any(a.startswith("logs:") for a in _actions(s))
    ]
    assert logs_statements, "expected a statement granting logs:* actions"

    expected_prefix = (
        "arn:aws:logs:us-west-2:123456789012:log-group:"
        f"/aws/bedrock-agentcore/runtimes/harness_{HARNESS_NAME}-"
    )
    for s in logs_statements:
        resources = s["Resource"]
        resources = [resources] if isinstance(resources, str) else resources
        assert all(r != "*" for r in resources), (
            f"logs statement {_actions(s)} must not use Resource '*'"
        )
        for r in resources:
            assert isinstance(r, str) and r.startswith(expected_prefix), (
                f"logs resource {r!r} must be scoped to this harness's log-group "
                f"prefix {expected_prefix!r}"
            )


def test_put_metric_data_is_namespace_conditioned() -> None:
    """PutMetricData takes no ARN, so the namespace condition IS the scope.

    Unconditioned, this action can publish to any namespace -- including
    poisoning the AWS/* service metrics other alarms read.
    """
    statements = _all_statements(_synth())
    metric_statements = [
        s for s in statements if "cloudwatch:PutMetricData" in _actions(s)
    ]
    assert metric_statements, "expected a cloudwatch:PutMetricData statement"
    for s in metric_statements:
        namespaces = (s.get("Condition") or {}).get("StringLike", {}).get(
            "cloudwatch:namespace"
        )
        assert namespaces, (
            "cloudwatch:PutMetricData must carry a StringLike "
            f"cloudwatch:namespace condition; got Condition={s.get('Condition')!r}"
        )
        values = [namespaces] if isinstance(namespaces, str) else list(namespaces)
        assert all(v.startswith("bedrock-agentcore") for v in values), (
            f"metric namespace condition {values!r} must be scoped to the "
            "harness runtime's own namespace"
        )


def test_resource_star_only_on_unscopable_actions() -> None:
    """The general invariant: no wildcard resource over anything scopable.

    Stated as a property rather than a count of allowed wildcard statements, so
    a data-access action can never ride along inside a statement that already
    had a legitimate ``"*"``. That co-location is the actual failure mode -- a
    reviewer sees a wildcard they recognise and skims the action list.
    """
    for s in _all_statements(_synth()):
        resources = s["Resource"]
        resources = [resources] if isinstance(resources, str) else resources
        if not any(r == "*" for r in resources):
            continue
        offenders = sorted(set(_actions(s)) - _UNSCOPABLE_ACTIONS)
        assert not offenders, (
            f"statement uses Resource '*' but contains scopable action(s) "
            f"{offenders}. Move them into their own ARN-scoped statement, or -- "
            f"if AWS genuinely provides no resource type for them -- add them to "
            f"_UNSCOPABLE_ACTIONS with that justification."
        )


def test_stack_harness_name_matches_provisioner() -> None:
    """The stack and the provisioner must agree on the harness name.

    They are separate programs: ``provision_harness.py`` names the harness, and
    the runtime derives its log-group name from that name, which this stack's
    IAM grant is scoped to. If they drift, synth and deploy both succeed and the
    harness simply loses logging -- no error anywhere. Same class of trap as the
    web-push bundle-image/runtime pair, so it gets the same kind of guard.
    """
    provision = (
        Path(__file__).resolve().parents[1] / "provision" / "provision_harness.py"
    )
    source = provision.read_text(encoding="utf-8")
    match = re.search(r'^HARNESS_NAME\s*=\s*"([^"]+)"', source, re.MULTILINE)
    assert match, f"could not find HARNESS_NAME in {provision}"
    assert match.group(1) == _CDK_JSON_CONTEXT["harnessName"], (
        f"provision_harness.py HARNESS_NAME={match.group(1)!r} disagrees with "
        f"cdk.json harnessName={_CDK_JSON_CONTEXT['harnessName']!r}. The IAM "
        "log-group scope is built from the cdk.json value, so a mismatch "
        "silently sends the grant at log groups that never exist."
    )


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


def test_outputs_present() -> None:
    template = _synth()
    outputs = template.to_json().get("Outputs", {})
    keys = set(outputs)
    assert "HarnessExecutionRoleArn" in keys
    assert "BedrockModelId" in keys
    assert "MemoryArn" in keys
