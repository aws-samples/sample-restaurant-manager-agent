# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""HarnessAgentStack - least-privilege execution role for the AgentCore
Managed Harness agent layer (spec 022 Task 1).

Provisions ONLY the harness execution role. The harness resource itself has no
stable CloudFormation construct at GA, so it is created out-of-band by
``provision/provision_harness.py`` (control plane) referencing the
``HarnessExecutionRoleArn`` output.

Security (spec 022 R5.1 / R5.4):
  - Trust: bedrock-agentcore.amazonaws.com (the harness runtime assumes this).
  - Bedrock model invoke scoped to the configured Sonnet inference profile +
    its foundation-model ARNs (NOT foundation-model/*).
  - AgentCore Memory data-plane scoped to the specific Memory resource ARN.
  - Observability is split by how narrowly each API can be scoped: CloudWatch
    Logs is pinned to this harness's own log-group prefix
    (``/aws/bedrock-agentcore/runtimes/harness_{harnessName}-*``), while the
    X-Ray actions, ``cloudwatch:PutMetricData`` and the ECR-public auth pair
    are resource-less APIs that admit no ARN -- PutMetricData is additionally
    conditioned to the ``bedrock-agentcore*`` metric namespace. A statement here
    may use ``"*"`` only when every action in it is resource-less.
  - DELIBERATELY NOT granted: bedrock-agentcore:InvokeAgentRuntime (raw command
    exec), bedrock-agentcore:InvokeGateway (inline tool path per OD1), any
    iam:* / admin / wildcard-service action, any Action: "*".

Region-locked to us-west-2 (co-location with the Memory resource, the gateways,
and IoT Core). ASCII-only role description (IAM rejects unicode dashes -- see
architecture-decisions.md).
"""
from __future__ import annotations

from typing import Final

import aws_cdk as cdk
from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_iam as iam
from cdk_nag import NagSuppressions
from constructs import Construct

REQUIRED_REGION: Final[str] = "us-west-2"

#: Regions the cross-region Sonnet inference profile may route to. Mirrors the
#: durable supervisor's Bedrock scoping (spec 014).
_FOUNDATION_MODEL_REGIONS: Final[tuple[str, ...]] = (
    "us-east-1",
    "us-west-2",
    "us-east-2",
)

_STACK_TAGS: Final[dict[str, str]] = {
    "Project": "restaurant-simulator",
    "Environment": "dev",
    "ManagedBy": "cdk",
    "Spec": "022-harness-agent-migration",
}


class HarnessAgentStack(Stack):
    """Least-privilege execution role for the managed-harness agent layer."""

    execution_role: iam.Role

    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)

        if self.region != REQUIRED_REGION:
            raise ValueError(
                "HarnessAgentStack must be deployed to "
                f"{REQUIRED_REGION!r}, got {self.region!r}. Spec 022 requires "
                "co-location with the AgentCore Memory resource, the gateway "
                "Lambdas, and IoT Core."
            )

        tags = cdk.Tags.of(self)
        for key, value in _STACK_TAGS.items():
            tags.add(key, value)

        memory_arn = self._require_context("memoryArn")
        model_id = self._require_context("bedrockModelId")
        role_name = self._require_context("executionRoleName")
        # Names this harness's log groups (see the ObservabilityLogs statement).
        # MUST equal provision_harness.py's HARNESS_NAME -- the provisioner names
        # the harness and the runtime derives the log-group name from it, so a
        # drift here silently scopes the grant to log groups that never exist,
        # and the harness loses logging with no synth or deploy error.
        # test_harness_agent_stack.py asserts the two agree.
        harness_name = self._require_context("harnessName")

        # Validate the Memory ARN belongs to this account/region (avoid the
        # cross-account-placeholder trap from architecture-decisions.md).
        expected_prefix = f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:memory/"
        if not memory_arn.startswith("arn:aws:bedrock-agentcore:"):
            raise ValueError(f"memoryArn is not a bedrock-agentcore ARN: {memory_arn!r}")
        # When account is resolved (deploy time), enforce the match. At synth
        # with an unresolved token, self.account is a token and the check is
        # skipped gracefully.
        if not self.account.startswith("${Token") and not memory_arn.startswith(
            expected_prefix
        ):
            raise ValueError(
                f"memoryArn {memory_arn!r} does not match this stack's "
                f"account/region prefix {expected_prefix!r}."
            )

        # --- Execution role -------------------------------------------------
        role = iam.Role(
            self,
            "HarnessExecutionRole",
            role_name=role_name,
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            description=(
                "AgentCore Managed Harness execution role (spec 022). Bedrock "
                "model invoke + scoped AgentCore Memory only. No "
                "InvokeAgentRuntime, no InvokeGateway, no broad permissions."
            ),
        )

        # (1) Bedrock model invoke - scoped to the configured Sonnet inference
        # profile + its foundation-model ARNs across the inference-profile
        # regions. The inference-profile id is the model_id with the regional
        # "us." prefix; the underlying foundation model strips that prefix.
        foundation_model_id = model_id.split(".", 1)[1] if model_id.startswith(
            ("us.", "eu.", "apac.")
        ) else model_id
        inference_profile_arn = (
            f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/{model_id}"
        )
        foundation_model_arns = [
            f"arn:aws:bedrock:{r}::foundation-model/{foundation_model_id}"
            for r in _FOUNDATION_MODEL_REGIONS
        ]
        role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockModelInvocation",
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:Converse",
                    "bedrock:ConverseStream",
                ],
                resources=[inference_profile_arn, *foundation_model_arns],
            )
        )

        # (2) AgentCore Memory data plane - scoped to the specific Memory ARN
        # (and its child records) ONLY. Same-session continuity (OD3).
        role.add_to_policy(
            iam.PolicyStatement(
                sid="AgentCoreMemoryDataPlane",
                actions=[
                    "bedrock-agentcore:CreateEvent",
                    "bedrock-agentcore:ListEvents",
                    "bedrock-agentcore:GetEvent",
                    "bedrock-agentcore:RetrieveMemoryRecords",
                    "bedrock-agentcore:GetMemoryRecord",
                    "bedrock-agentcore:ListMemoryRecords",
                ],
                resources=[memory_arn, f"{memory_arn}/*"],
            )
        )

        # (3) Observability - required for the managed harness runtime to emit
        # traces/metrics/logs (per-step trace visibility, spec 022 R6). Split
        # into three statements by how narrowly each API can actually be
        # scoped, rather than one convenient wildcard: a statement may use
        # Resource "*" only when EVERY action in it is a resource-less API (the
        # invariant from architecture-decisions.md, "no wildcard resource over
        # DATA"). Keeping the scopable logs actions in their own statement is
        # what stops a future edit from quietly adding a data action alongside
        # a "*" it inherited.
        #
        # (3a) CloudWatch Logs - scoped to this harness's own log groups. The
        # runtime names them deterministically from the harness name:
        #   /aws/bedrock-agentcore/runtimes/harness_{harnessName}-{runtimeId}-DEFAULT
        # Only the 10-character runtimeId varies (it changes on re-provision and
        # is NOT the harnessId), so the trailing wildcard covers it. Verified
        # against CloudTrail: this role's CreateLogGroup calls target exactly
        # this prefix. The trailing "*" also spans the ":log-stream:..." suffix,
        # but both ARN forms are listed so the intent survives a reader.
        log_group_prefix = (
            f"/aws/bedrock-agentcore/runtimes/harness_{harness_name}-"
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ObservabilityLogs",
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogGroups",
                    "logs:DescribeLogStreams",
                ],
                resources=[
                    f"arn:aws:logs:{self.region}:{self.account}:log-group:{log_group_prefix}*",
                    f"arn:aws:logs:{self.region}:{self.account}:log-group:{log_group_prefix}*:*",
                ],
            )
        )

        # (3b) X-Ray tracing - all four actions are resource-less APIs (the
        # service authorization reference lists no resource type for any of
        # them), so "*" is the only valid scope. Nothing here reads or writes
        # application data.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ObservabilityTracing",
                actions=[
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "xray:GetSamplingRules",
                    "xray:GetSamplingTargets",
                ],
                resources=["*"],
            )
        )

        # (3c) CloudWatch metrics - PutMetricData also takes no resource ARN,
        # but it IS conditionable on the target namespace, which is the same
        # mitigation SimulatorEc2AlbStack uses for its per-tool metrics. The
        # managed harness runtime publishes its Strands event-loop metrics to
        # the "bedrock-agentcore" namespace (confirmed against the live
        # account); StringLike rather than StringEquals so a future
        # "bedrock-agentcore/<something>" sub-namespace does not silently start
        # failing to publish. Without the condition this action can write to
        # ANY namespace, including overwriting AWS/* service metrics.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ObservabilityMetrics",
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={
                    "StringLike": {"cloudwatch:namespace": "bedrock-agentcore*"}
                },
            )
        )

        # (4) ECR-public pull - the managed harness runtime pulls its default
        # container image from ECR Public at session start.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="EcrPublicPullForDefaultHarnessImage",
                actions=[
                    "ecr-public:GetAuthorizationToken",
                    "sts:GetServiceBearerToken",
                ],
                resources=["*"],
            )
        )

        # DELIBERATELY ABSENT (asserted by tests/test_harness_agent_stack.py):
        #   bedrock-agentcore:InvokeAgentRuntime (raw command exec, R5.4)
        #   bedrock-agentcore:InvokeGateway      (inline tool path, OD1)
        #   any iam:* / sts:AssumeRole / admin / "*" service action
        #   any Action: "*"

        self.execution_role = role

        # ------------------------------------------------------------------
        # cdk-nag suppressions (P1). AwsSolutions-IAM5 objects to wildcard
        # RESOURCES. All three wildcards below are accepted because the APIs
        # involved do not support resource-level permissions, or because the
        # wildcard is a child-record scope under one specific resource -- not
        # because narrowing them would be inconvenient.
        #
        # Note what is NOT suppressed: the role has no Action wildcard, no
        # iam:*, no sts:AssumeRole, and neither InvokeAgentRuntime nor
        # InvokeGateway (asserted by tests/test_harness_agent_stack.py). IAM5 is
        # about resource scope; those absences are the actual privilege boundary.
        # ------------------------------------------------------------------
        # Note: apply_to_children=True is REQUIRED here. Both findings are raised
        # on the role's generated `DefaultPolicy/Resource` child, not on the role
        # itself, so a suppression attached only to the role silently fails to
        # match and synth keeps failing with the same two errors.
        NagSuppressions.add_resource_suppressions(
            role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "AgentCore Memory child-record scope. The grant is already "
                        "pinned to ONE memory resource; the '/*' suffix covers that "
                        "memory's sessions/events/records, which are addressed as "
                        "sub-resources and cannot be enumerated at synth time. It "
                        "confers nothing outside this memory, and the sibling "
                        "statement is pinned to the bare ARN."
                    ),
                    "appliesTo": [f"Resource::{memory_arn}/*"],
                },
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "Harness log-group prefix. The managed harness runtime "
                        "names its log group "
                        "/aws/bedrock-agentcore/runtimes/harness_{harnessName}-"
                        "{runtimeId}-DEFAULT, where only the 10-character runtimeId "
                        "is unknown at synth time (it is assigned per runtime and "
                        "changes on re-provision), so the trailing wildcard is the "
                        "tightest expressible scope. It cannot reach any other "
                        "service's or stack's log groups, and the actions are "
                        "log-write plus two log-metadata reads."
                    ),
                    "appliesTo": [
                        f"Resource::arn:aws:logs:{self.region}:{self.account}"
                        f":log-group:{log_group_prefix}*",
                        f"Resource::arn:aws:logs:{self.region}:{self.account}"
                        f":log-group:{log_group_prefix}*:*",
                    ],
                },
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "Only resource-less APIs remain on '*': "
                        "cloudwatch:PutMetricData and the xray:Put*/Get*Sampling* "
                        "actions take no resource ARN at all, and "
                        "ecr-public:GetAuthorizationToken / sts:GetServiceBearerToken "
                        "are account-level token requests. PutMetricData is "
                        "further conditioned to the bedrock-agentcore* metric "
                        "namespace. The logs:* actions are NO LONGER here -- they "
                        "are pinned to this harness's own log-group prefix in the "
                        "ObservabilityLogs statement. None of these can read or "
                        "mutate application data. To unblock this suppression, an "
                        "AWS change adding a resource type to one of these APIs "
                        "would let that action move to a scoped statement."
                    ),
                    "appliesTo": ["Resource::*"],
                },
            ],
            apply_to_children=True,
        )

        # --- Outputs --------------------------------------------------------
        CfnOutput(
            self,
            "HarnessExecutionRoleArn",
            value=role.role_arn,
            description=(
                "Execution role ARN to pass to provision_harness.py "
                "(--role-arn) when creating the harness."
            ),
        )
        CfnOutput(
            self,
            "BedrockModelId",
            value=model_id,
            description="Default model id the harness/role are scoped to.",
        )
        CfnOutput(
            self,
            "MemoryArn",
            value=memory_arn,
            description="AgentCore Memory resource attached to the harness.",
        )

    def _require_context(self, key: str) -> str:
        value = self.node.try_get_context(key)
        if value is None or not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"Missing required CDK context value {key!r}. Set it in "
                f"cdk.json or pass -c {key}=<value> at deploy time."
            )
        return value
