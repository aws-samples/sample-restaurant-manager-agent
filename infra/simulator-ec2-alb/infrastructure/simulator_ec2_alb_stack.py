# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CDK stack for the simulator EC2 + ALB deployment.

Per spec 004, this stack is pinned to AWS region ``us-west-2`` (requirements
R1.1 and R1.3) and applies a uniform set of tags to every resource it creates
(requirement R1.4). Task 2.1 adds the VPC topology (requirements R2.1–R2.5);
task 3 adds the three security groups (``alb-sg``, ``ec2-sg``, ``vpce-sg``)
with the reference graph ALB → EC2 → VPC endpoints (requirements R3.1–R3.6);
tasks 5 and 6 add the Secrets Manager placeholder, the two CloudWatch log
groups, and the IAM role + instance profile that the EC2 instance consumes
(requirements R6.1–R6.5, R8.1–R8.2, R9.1–R9.2). Tasks 4.4, 7, and 8 add the
source asset, the EC2 instance with its rendered user-data, and the
Application Load Balancer (listener + target group, requirements R4.*, R5.*,
R7.*).

Spec 007 added ALB-terminated Cognito auth (HTTPS:443 listener + Route 53 +
ACM); spec 029 **supersedes** that: TLS and Cognito auth now terminate at
Amazon CloudFront via a Lambda@Edge (``cognito-at-edge``) authenticator on the
distribution's viewer-request event. The ALB is internal and HTTP-only, reached
only through a CloudFront VPC origin; the domain / Route 53 / ACM constructs are
removed (browser-trusted TLS uses CloudFront's default ``*.cloudfront.net``
cert). The edge function's config (Cognito IDs + CSRF nonce secret) is injected
at synth from ``edgeCognito*`` context into a gitignored ``config.local.json``
sidecar, implying a two-phase bootstrap deploy. ``us-east-1`` must be
``cdk bootstrap``-ed for the Lambda@Edge support stack. See
the CloudFront edge-auth design record.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import aws_cdk as cdk
from aws_cdk import (
    CfnOutput,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cloudwatch as cloudwatch,
    aws_cognito as cognito,
    aws_ec2 as ec2,
    aws_elasticloadbalancingv2 as elbv2,
    aws_elasticloadbalancingv2_targets as elbv2_targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3_assets as s3_assets,
    custom_resources as cr,
)
from cdk_nag import NagSuppressions
from constructs import Construct

#: Workspace root (where ``main.py`` and ``pyproject.toml`` live). The stack
#: file is at ``infra/simulator-ec2-alb/infrastructure/simulator_ec2_alb_stack.py``
#: so the repository root is three directories up.
_WORKSPACE_ROOT: Final[Path] = Path(__file__).resolve().parents[3]

#: Directory holding the user-data script, systemd unit, and CW Agent config.
_SCRIPTS_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "scripts"

#: Region this stack is allowed to deploy into. Spec 004 (R1.1, R1.3).
REQUIRED_REGION: Final[str] = "us-west-2"

#: Context flags that assert "this is a real deployment, not a bootstrap."
#:
#: Both default to **true** so the safe behaviour is the default one: if a
#: feature's account-specific context values are missing, synth fails loudly
#: instead of silently dropping the inline policy (or the edge authenticator)
#: that the live deployment depends on. That is the trap described in
#: architecture-decisions.md § "CDK stack context flags: empty-default gates
#: are traps at deploy time" -- a bare ``cdk deploy`` with the value absent
#: would otherwise destroy live configuration and report success.
#:
#: These replaced an allowlist of AWS account ids, which used account identity
#: as a proxy for "deploying for real." The flag says the same thing directly,
#: cannot be forgotten by deploying from a different account, and keeps no
#: account identifier in source. Bootstrapping an account that genuinely does
#: not have the dependency yet is the explicit opt-out:
#:
#:     cdk deploy -c chatFeatureRequired=false
#:     cdk deploy -c edgeAuthRequired=false
#:
#: The values these flags guard are account-specific, so they live in the
#: gitignored ``cdk.context.json`` (or a ``-c`` flag) rather than in the
#: committed ``cdk.json`` -- see this stack's README.
CHAT_FEATURE_REQUIRED_FLAG: Final[str] = "chatFeatureRequired"
EDGE_AUTH_REQUIRED_FLAG: Final[str] = "edgeAuthRequired"


def _flag_is_set(node: Any, key: str, *, default: bool = True) -> bool:
    """Read a boolean context flag that may arrive as a bool or a string.

    ``cdk.json`` yields a real bool; ``-c key=false`` yields the *string*
    ``"false"``, which is truthy in Python -- so a bare ``bool(...)`` would
    make the documented opt-out silently ineffective.
    """
    raw = node.try_get_context(key)
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in {"false", "0", "no", "off"}


#: Tags applied to every resource in the stack. Spec 004 (R1.4).
STACK_TAGS: Final[dict[str, str]] = {
    "Project": "restaurant-simulator",
    "Environment": "dev",
    "ManagedBy": "cdk",
    "Spec": "004-simulator-ec2-alb-cdk",
}


def _validate_arn_account(arn: str, expected_account: str, context_key: str) -> None:
    """Raise ``ValueError`` if ``arn``'s account segment doesn't match.

    Per architecture-decisions.md § "CDK context defaults must match the
    deploy account," a stack that reads a cross-account ARN via context
    must compare the ARN's account segment against ``self.account`` at
    synth time. Otherwise a ``cdk.json`` that shipped with a placeholder
    like ``000000000000`` silently breaks cross-stack IAM at deploy time.
    """
    # ARN format: arn:aws:<service>:<region>:<account>:<resource>
    segments = arn.split(":", 5)
    if len(segments) < 6:
        raise ValueError(
            f"{context_key}={arn!r} is not a valid ARN (expected 6 colon-"
            "delimited segments)"
        )
    account_segment = segments[4]
    if account_segment != expected_account:
        raise ValueError(
            f"{context_key} account segment {account_segment!r} does not "
            f"match the deploy account {expected_account!r}. If you are "
            "deploying to a new account, update cdk.json's default to an "
            "ARN in that account, or pass -c "
            f"{context_key}=<correct-arn> on the command line."
        )


class SimulatorEc2AlbStack(cdk.Stack):
    """Infrastructure stack for hosting the simulator on EC2 behind an ALB.

    The stack is region-locked to ``us-west-2``. Attempting to instantiate it
    against any other region raises :class:`ValueError` at synth time, well
    before CloudFormation is ever contacted.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # R1.1 / R1.3: refuse to synthesize outside us-west-2. We check
        # ``self.region`` rather than ``kwargs["env"]`` so this also catches
        # the case where the region was supplied via CDK context, environment
        # variables, or a default-resolved agnostic stack.
        if self.region != REQUIRED_REGION:
            raise ValueError(
                "SimulatorEc2AlbStack must be deployed in "
                f"{REQUIRED_REGION!r}, got {self.region!r}"
            )

        # R1.4: apply the standard tag set to every resource in the stack.
        # ``Tags.of(self)`` propagates tags to every taggable construct added
        # to the stack, including those introduced by later tasks.
        tags = cdk.Tags.of(self)
        for key, value in STACK_TAGS.items():
            tags.add(key, value)

        # R2.1–R2.5: VPC topology for the simulator. Two AZs give the ALB
        # the redundancy it requires (ALB must span at least two subnets in
        # different AZs) and leave a second private subnet available for a
        # future HA move without re-architecting the network.
        #
        # A single NAT Gateway (``nat_gateways=1``) is a deliberate non-prod
        # cost decision documented in ``design.md``. Production usage would
        # move to one NAT per AZ.
        self.vpc = ec2.Vpc(
            self,
            "SimulatorVpc",
            ip_addresses=ec2.IpAddresses.cidr("10.0.0.0/16"),
            max_azs=2,
            nat_gateways=1,
            enable_dns_hostnames=True,
            enable_dns_support=True,
            # Network visibility (cdk-nag AwsSolutions-VPC7, P1). Fixed rather
            # than suppressed: this VPC carries the store round-trip and the
            # agent's outbound calls, so "what talked to what" is the one thing
            # you cannot reconstruct after an incident. Retained 1 month to keep
            # the cost proportional to a dev harness.
            flow_logs={
                "cloudwatch": ec2.FlowLogOptions(
                    destination=ec2.FlowLogDestination.to_cloud_watch_logs(),
                    traffic_type=ec2.FlowLogTrafficType.ALL,
                )
            },
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
                ec2.SubnetConfiguration(
                    name="private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
            ],
        )

        # Spec 029 (R2.3, R7): the ALB is now an INTERNAL, HTTP-only origin
        # reachable only through CloudFront via a VPC origin. It has no public
        # ingress; its single ingress rule - HTTP:80 from CloudFront's managed
        # VPC-origins service security group - is added after the distribution
        # is created (see the CloudFront section below). Spec 007's
        # ``0.0.0.0/0`` ingress on 443/80 is intentionally removed.
        self.alb_sg = ec2.SecurityGroup(
            self,
            "AlbSg",
            vpc=self.vpc,
            description="Simulator ALB - HTTP:80 from CloudFront VPC origin only",
            allow_all_outbound=True,
        )

        # R3.3, R3.4, R3.5: EC2 SG — only :8000 from the ALB SG. No SSH
        # ingress from any source. Egress is default-open so the instance
        # can reach package mirrors, Git, and AWS APIs via the NAT Gateway
        # and the VPC interface endpoints.
        self.ec2_sg = ec2.SecurityGroup(
            self,
            "Ec2Sg",
            vpc=self.vpc,
            description="Simulator EC2 - inbound app traffic from the ALB only",
            allow_all_outbound=True,
        )
        self.ec2_sg.add_ingress_rule(
            peer=self.alb_sg,
            connection=ec2.Port.tcp(8000),
            description="App traffic from the ALB",
        )

        # R3.6: VPC endpoints SG — TCP 443 from the EC2 SG so the instance
        # can reach SSM, SSM Messages, EC2 Messages, and Secrets Manager
        # over PrivateLink.
        self.vpce_sg = ec2.SecurityGroup(
            self,
            "VpceSg",
            vpc=self.vpc,
            description="Simulator VPC interface endpoints - HTTPS from EC2 SG",
            allow_all_outbound=True,
        )
        self.vpce_sg.add_ingress_rule(
            peer=self.ec2_sg,
            connection=ec2.Port.tcp(443),
            description="HTTPS from EC2 SG to interface endpoints",
        )

        # R2.6: VPC interface endpoints so the instance can reach SSM and
        # Secrets Manager without traversing the NAT Gateway. Using
        # ``private_dns_enabled=True`` lets the service clients resolve the
        # normal public endpoint hostnames (e.g. ``ssm.us-west-2.amazonaws.com``)
        # to the ENI IPs transparently, so no SDK configuration is required
        # on the instance. All endpoints are placed in the private subnets
        # and attached to ``vpce_sg``.
        private_subnets = ec2.SubnetSelection(
            subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
        )

        self.ssm_endpoint = ec2.InterfaceVpcEndpoint(
            self,
            "SsmEndpoint",
            vpc=self.vpc,
            service=ec2.InterfaceVpcEndpointAwsService.SSM,
            subnets=private_subnets,
            security_groups=[self.vpce_sg],
            private_dns_enabled=True,
        )
        self.ssm_messages_endpoint = ec2.InterfaceVpcEndpoint(
            self,
            "SsmMessagesEndpoint",
            vpc=self.vpc,
            service=ec2.InterfaceVpcEndpointAwsService.SSM_MESSAGES,
            subnets=private_subnets,
            security_groups=[self.vpce_sg],
            private_dns_enabled=True,
        )
        self.ec2_messages_endpoint = ec2.InterfaceVpcEndpoint(
            self,
            "Ec2MessagesEndpoint",
            vpc=self.vpc,
            service=ec2.InterfaceVpcEndpointAwsService.EC2_MESSAGES,
            subnets=private_subnets,
            security_groups=[self.vpce_sg],
            private_dns_enabled=True,
        )
        self.secrets_manager_endpoint = ec2.InterfaceVpcEndpoint(
            self,
            "SecretsManagerEndpoint",
            vpc=self.vpc,
            service=ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
            subnets=private_subnets,
            security_groups=[self.vpce_sg],
            private_dns_enabled=True,
        )

        # ``InterfaceVpcEndpoint`` (L2) does not propagate stack-level tags
        # to the underlying ``AWS::EC2::VPCEndpoint`` — aws-cdk-lib doesn't
        # mark ``CfnVPCEndpoint`` as ``ITaggable``. R1.4 requires every
        # resource to carry the four standard tags, so we reach down to the
        # ``CfnVPCEndpoint`` via the L1 escape hatch and apply them
        # explicitly. ``add_property_override`` on ``Tags`` would also work
        # but is less idiomatic than an L1 ``tags.set_tag`` call.
        for endpoint in (
            self.ssm_endpoint,
            self.ssm_messages_endpoint,
            self.ec2_messages_endpoint,
            self.secrets_manager_endpoint,
        ):
            cfn_endpoint: ec2.CfnVPCEndpoint = endpoint.node.default_child  # type: ignore[assignment]
            cfn_endpoint.add_property_override(
                "Tags",
                [{"Key": k, "Value": v} for k, v in STACK_TAGS.items()],
            )

        # R8.1 (amended 2026-09-13): this stack creates NO Secrets Manager secret.
        #
        # It used to create ``restaurant-sim/mqtt/credentials`` as a placeholder for
        # Amazon MQ username/password credentials. That broker was decommissioned in
        # spec 020 / ADR-004 when the transport moved to AWS IoT Core with per-store
        # X.509 mTLS, so the secret held ``{}`` in every deployment and nothing read
        # it -- while still carrying a live ``GetSecretValue`` grant on the EC2 role
        # and an unreachable user-data branch. All three were removed together.
        #
        # The credential the instance DOES read is the IoT device certificate bundle
        # at ``restaurant-sim/iot/{store_id}/cert`` (R8.2). It is deliberately owned
        # by ``infra/iot-core-broker`` and not by this stack: a per-store credential
        # has a per-store lifecycle, and ``cdk destroy`` here must not be able to
        # revoke a store's identity. See ``ReadIotCertSecret`` below for the grant.

        # R9.1, R9.2: CloudWatch log groups for the two streams the
        # CloudWatch Agent pushes from the instance — bootstrap output and
        # the systemd-managed application log. 14-day retention keeps costs
        # low for a dev environment while still giving enough history to
        # debug a bad deploy. ``RemovalPolicy.DESTROY`` lets ``cdk destroy``
        # remove the groups cleanly.
        self.log_group_user_data = logs.LogGroup(
            self,
            "UserDataLogGroup",
            log_group_name="/simulator/user-data",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        self.log_group_app = logs.LogGroup(
            self,
            "AppLogGroup",
            log_group_name="/simulator/app",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # R6.1–R6.4: IAM role assumed by the EC2 service. The managed
        # ``AmazonSSMManagedInstanceCore`` policy is what enables SSM
        # Session Manager access, which is how we reach the box without
        # ever opening port 22 (R3.5). The two inline policies are narrow:
        #   * ``WriteSimulatorLogs`` — log-stream writes scoped to the two
        #     log groups owned by this stack (R6.4). The ``:*`` suffix is
        #     required because CloudWatch Logs scopes stream-level actions
        #     to ARNs of the form ``<log-group-arn>:log-stream:*``.
        self.ec2_role = iam.Role(
            self,
            "Ec2Role",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            description=(
                "EC2 role for the simulator instance: SSM Session Manager "
                "access plus narrowly scoped Secrets Manager and CloudWatch "
                "Logs permissions."
            ),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSSMManagedInstanceCore"
                ),
            ],
            inline_policies={
                # R6.3 (amended 2026-09-13): the former ``ReadMqttSecret`` grant on the
                # retired Amazon MQ placeholder secret is gone. The role's ONLY
                # Secrets Manager grant is ``ReadIotCertSecret`` (attached below),
                # scoped to the one IoT certificate secret the instance actually reads.
                "WriteSimulatorLogs": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=[
                                "logs:CreateLogStream",
                                "logs:PutLogEvents",
                                "logs:DescribeLogStreams",
                                # The CloudWatch Agent calls DescribeLogGroups
                                # on startup to read each destination's
                                # retention. Without this, the agent logs a
                                # stream of AccessDenied errors and still
                                # publishes events, but the noise makes real
                                # failures hard to spot.
                                "logs:DescribeLogGroups",
                            ],
                            resources=[
                                self.log_group_user_data.log_group_arn,
                                f"{self.log_group_user_data.log_group_arn}:*",
                                self.log_group_app.log_group_arn,
                                f"{self.log_group_app.log_group_arn}:*",
                            ],
                        ),
                    ],
                ),
            },
        )

        # R6.5: The instance profile is created implicitly by the CDK
        # ``ec2.Instance`` construct below when we pass ``role=self.ec2_role``.
        # That is the CDK-recommended pattern and avoids hand-wiring an
        # ``iam.CfnInstanceProfile``.

        # --------------------------------------------------------------
        # Chat-feature context guard.
        # --------------------------------------------------------------
        #
        # The durable supervisor was decommissioned in spec 022 Task 6, so the
        # former ``supervisorLambdaAliasArn`` context key and the
        # ``InvokeDurableSupervisor`` inline policy are gone; the chat path now
        # runs entirely on the AgentCore Managed Harness (see ``harnessArn``
        # below). ``agentcoreMemoryId`` is still gated the same way.
        #
        # An empty ``agentcoreMemoryId`` means "no memory yet" and the
        # ChatMemoryAccess policy is skipped so this stack stays deployable on a
        # fresh account -- but see the ``chatFeatureRequired`` flag: on a real
        # deployment an empty value is a bug, not a bootstrap state. A bare
        # ``cdk deploy`` without the context value would silently destroy the
        # live inline policy and break the chat path
        # (architecture-decisions.md § "CDK stack context flags:
        # empty-default gates are traps at deploy time").
        memory_id_early_peek: str = (
            self.node.try_get_context("agentcoreMemoryId") or ""
        ).strip()

        # Guard: unless this synth is explicitly flagged as a bootstrap, refuse
        # to proceed with the value empty. This catches the "bare cdk deploy
        # would destroy the gated policy" foot-gun early, at synth time, before
        # any destructive diff is shown.
        if (
            _flag_is_set(self.node, CHAT_FEATURE_REQUIRED_FLAG)
            and not memory_id_early_peek
        ):
            raise ValueError(
                "SimulatorEc2AlbStack is being synthesized with "
                f"'{CHAT_FEATURE_REQUIRED_FLAG}' set (the default), but the CDK "
                "context key 'agentcoreMemoryId' is empty. This key gates the "
                "ChatMemoryAccess inline policy on the EC2 role; deploying "
                "without it would destroy the live policy and break chat "
                "history. Set it in the gitignored cdk.context.json or pass "
                "`cdk deploy -c agentcoreMemoryId=<id>`. If you are "
                "bootstrapping an account that genuinely does not have "
                "AgentCore Memory yet, opt out explicitly with "
                f"`-c {CHAT_FEATURE_REQUIRED_FLAG}=false`."
            )

        # --------------------------------------------------------------
        # Spec 015 follow-up: additive IAM grant for the AgentCore Memory
        # that backs the chat-history endpoint. Spec 008 never wired this
        # into the EC2 role (the chat feature worked in dev only because
        # developer creds had broad IAM); spec 015's end-to-end validation
        # surfaced the gap when /api/chat/history returned 502.
        #
        # The memory id is passed via the ``agentcoreMemoryId`` CDK
        # context key (account-specific, so it lives in the gitignored
        # ``cdk.context.json`` or a ``-c`` flag -- not in the committed
        # ``cdk.json``). An empty string means "no memory configured" and the
        # policy is skipped so this stack stays deployable while bootstrapping
        # -- but the ``chatFeatureRequired`` guard above (default true) refuses
        # to synthesize with an empty value unless that bootstrap case is
        # declared explicitly.
        memory_id = memory_id_early_peek
        if memory_id:
            memory_arn = (
                f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:"
                f"memory/{memory_id}"
            )
            self.ec2_role.attach_inline_policy(
                iam.Policy(
                    self,
                    "ChatMemoryAccess",
                    statements=[
                        iam.PolicyStatement(
                            actions=[
                                "bedrock-agentcore:CreateEvent",
                                "bedrock-agentcore:ListEvents",
                            ],
                            resources=[
                                memory_arn,
                                f"{memory_arn}/*",
                            ],
                        ),
                    ],
                ),
            )

        # --------------------------------------------------------------
        # Task 4.4: render user-data and publish the repo as an S3 asset.
        # --------------------------------------------------------------
        #
        # The instance holds no source-control credentials (and the source
        # repository may well be private), so ``git clone`` from the instance
        # is not an option. Instead, CDK
        # zips the workspace into a bootstrap-bucket asset at synth time,
        # and user-data fetches the ZIP with ``aws s3 cp``. The asset is
        # scoped to the workspace root so everything the instance needs
        # (``main.py``, ``simulator/``, ``frontend/``, the CDK-owned
        # ``infra/simulator-ec2-alb/scripts/`` files referenced by
        # user-data) travels in one bundle.
        #
        # Excludes keep the archive small and deterministic: virtualenvs,
        # build caches, node_modules (rebuilt on the instance),
        # ``cdk.out`` (would create a bootstrap loop), and OS cruft.
        self.repo_asset = s3_assets.Asset(
            self,
            "RepoAsset",
            path=str(_WORKSPACE_ROOT),
            ignore_mode=cdk.IgnoreMode.GIT,
            exclude=[
                ".venv/",
                "__pycache__/",
                "node_modules/",
                ".git/",
                "cdk.out/",
                ".pytest_cache/",
                "*.egg-info/",
                ".DS_Store",
                "frontend/dist/",
                "infra/simulator-ec2-alb/.venv/",
                "infra/simulator-ec2-alb/cdk.out/",
            ],
        )
        # The instance needs read access to the asset bucket to fetch the
        # bundle in user-data. ``grant_read`` scopes this to just the
        # asset's object, not the whole bootstrap bucket.
        self.repo_asset.grant_read(self.ec2_role)

        # spec 020: the EC2 simulator fetches its IoT device cert bundle from
        # Secrets Manager at boot (mtls cutover). The secret name is
        # deterministic (provision/provision_store.py). Grant read on it; the
        # ``-*`` suffix covers Secrets Manager's random 6-char ARN suffix.
        iot_cert_secret_id = (
            self.node.try_get_context("iotCertSecretId")
            or "restaurant-sim/iot/sim-store-001/cert"
        )
        self.ec2_role.attach_inline_policy(
            iam.Policy(
                self,
                "ReadIotCertSecret",
                statements=[
                    iam.PolicyStatement(
                        actions=["secretsmanager:GetSecretValue"],
                        resources=[
                            f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:{iot_cert_secret_id}-*"
                        ],
                    ),
                ],
            ),
        )

        # --------------------------------------------------------------
        # Spec 022 Task 4 (corrected Task 5): harness-backend caller grants
        # (additive, UNCONDITIONAL). The harness is the only chat backend.
        # Constructed name-prefix / topic wildcards (no per-deploy context
        # value) avoid the empty-default gate trap.
        #
        # IMPORTANT (live finding 2026-06-19): the GA ``invoke_harness`` API
        # requires BOTH ``bedrock-agentcore:InvokeHarness`` AND
        # ``bedrock-agentcore:InvokeAgentRuntime`` on the *harness* resource ARN
        # (granting only one fails at invoke time on the other). This is the
        # LLM-mediated path; least privilege is preserved by scoping to the
        # harness NAME PREFIX in this account (never Resource "*" and never a
        # bare runtime ARN), so the role cannot invoke arbitrary AgentCore
        # runtimes -- only this project's harness.
        self.ec2_role.attach_inline_policy(
            iam.Policy(
                self,
                "InvokeAgentHarness",
                statements=[
                    iam.PolicyStatement(
                        actions=[
                            "bedrock-agentcore:InvokeHarness",
                            "bedrock-agentcore:InvokeAgentRuntime",
                        ],
                        resources=[
                            f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:harness/restaurant_manager_agent-*"
                        ],
                    ),
                ],
            ),
        )
        # Correlation A (spec 022 design §4): the backend publishes a command
        # to stores/{id}/commands/{domain} and SUBSCRIBES (outbound) to
        # stores/{id}/responses/{cid} over MQTT-over-WebSocket SigV4 to receive
        # the store's reply. Every grant is outbound-initiated; none creates an
        # inbound path into the store, and no thing/rule-management action is
        # granted (SP2 — the security boundary is preserved). Scoped to the
        # store topic tree.
        #
        # TM-01: scoped to THIS host's store, not `stores/*`. The fleet-wide
        # wildcard meant that if application logic ever routed a tool call at
        # another store -- which it did, via a model-supplied `store_id` -- IAM
        # would happily publish it. Pinning the topic makes the platform deny a
        # cross-tenant publish even if the application is wrong again, which is
        # the only one of the three TM-01 controls that does not depend on
        # application code being correct.
        store_id: str = str(
            self.node.try_get_context("storeId") or "sim-store-001"
        ).strip()
        if not store_id:
            raise ValueError(
                "storeId context is empty. It scopes the EC2 role's IoT topic "
                "grants; an empty value would widen them to the whole fleet."
            )
        self.ec2_role.attach_inline_policy(
            iam.Policy(
                self,
                "StoreIotRoundTrip",
                statements=[
                    iam.PolicyStatement(
                        sid="IotConnectChatClient",
                        actions=["iot:Connect"],
                        resources=[
                            f"arn:aws:iot:{self.region}:{self.account}:client/chat-harness-*"
                        ],
                    ),
                    iam.PolicyStatement(
                        sid="IotPublishStoreCommands",
                        actions=["iot:Publish"],
                        resources=[
                            # semgrep false positive: intentional implicit concatenation: one string split across lines, not a missing comma.
                            # nosemgrep: string-concat-in-list
                            f"arn:aws:iot:{self.region}:{self.account}"
                            f":topic/stores/{store_id}/commands/*"
                        ],
                    ),
                    iam.PolicyStatement(
                        sid="IotSubscribeStoreResponses",
                        actions=["iot:Subscribe"],
                        resources=[
                            # nosemgrep: string-concat-in-list
                            f"arn:aws:iot:{self.region}:{self.account}"
                            f":topicfilter/stores/{store_id}/responses/*"
                        ],
                    ),
                    iam.PolicyStatement(
                        sid="IotReceiveStoreResponses",
                        actions=["iot:Receive"],
                        resources=[
                            # nosemgrep: string-concat-in-list
                            f"arn:aws:iot:{self.region}:{self.account}"
                            f":topic/stores/{store_id}/responses/*"
                        ],
                    ),
                ],
            ),
        )

        # AGENTPERF06: best-effort per-tool CloudWatch telemetry emitted from
        # the tool-invocation layer (latency, error rate, timeouts). The
        # PutMetricData API cannot be resource-scoped, so it is conditioned to
        # the project's metric-namespace prefix. Emission is env-gated
        # (TOOL_METRICS_NAMESPACE) and runs off the critical path
        # (simulator/api/harness_tools.py::_emit_tool_metric); the grant is
        # harmless when metrics are disabled.
        self.ec2_role.attach_inline_policy(
            iam.Policy(
                self,
                "PublishToolMetrics",
                statements=[
                    iam.PolicyStatement(
                        sid="CloudWatchPutToolMetrics",
                        actions=["cloudwatch:PutMetricData"],
                        resources=["*"],
                        conditions={
                            "StringLike": {"cloudwatch:namespace": "RestaurantSim/*"}
                        },
                    ),
                ],
            ),
        )

        # Render the user-data script. The script holds three placeholders
        # that CDK resolves at synth time: the S3 URI of the repo asset,
        # the MQTT secret ARN, and the deploy region. All three values are
        # CloudFormation tokens (strings like ``${Token[TOKEN.123]}``) that
        # CloudFormation resolves to concrete values during deploy; a
        # plain ``str.replace`` is the idiomatic way to splice them into
        # a shell script here (``cdk.Fn.sub`` would also work but adds
        # complexity we don't need).
        # Harness wiring (spec 022). The AgentCore Managed Harness is the only
        # chat backend: the ``agentBackend`` selector and its ``durable``
        # alternative were removed with the durable agent layer in Task 6, so
        # ``harnessArn`` is now unconditionally required rather than required
        # only when the selector said "harness". ``iotEndpoint`` feeds the
        # harness inline-tool path. Both are rendered into
        # /etc/restaurant-sim/env.
        harness_arn: str = (self.node.try_get_context("harnessArn") or "").strip()
        iot_endpoint: str = (self.node.try_get_context("iotEndpoint") or "").strip()
        if not harness_arn:
            raise ValueError(
                "harnessArn context value is required (the provisioned harness "
                "ARN) -- it is the only chat backend since spec 022 Task 6 "
                "removed the durable supervisor. Set it in cdk.json or pass "
                "-c harnessArn=<arn>."
            )
        # Catch a cdk.json default left pointing at another account, which would
        # otherwise fail only at invoke time with AccessDenied.
        _validate_arn_account(harness_arn, self.account, "harnessArn")

        # ``iotEndpoint`` is account-specific (the account's IoT ATS endpoint)
        # and carries no default, so require it here rather than rendering an
        # empty value into /etc/restaurant-sim/env, where it would surface much
        # later as a store tool-call failure at runtime.
        if not iot_endpoint:
            raise ValueError(
                "iotEndpoint context value is required (this account's IoT ATS "
                "endpoint -- `aws iot describe-endpoint "
                "--endpoint-type iot:Data-ATS`). It feeds the harness "
                "inline-tool path. Set it in the gitignored cdk.context.json or "
                "pass -c iotEndpoint=<endpoint>."
            )

        # spec 025 Task 1f: voice edge (Nova Sonic) config. voiceEnabled flips
        # VOICE_ENABLED on the deployed display; vapidPublicKey is the Web
        # Push application-server PUBLIC key served to the browser (the private
        # key stays in the cloud Secrets Manager secret, never on the EC2).
        voice_enabled: str = (self.node.try_get_context("voiceEnabled") or "true").strip()
        voice_model_id: str = (
            self.node.try_get_context("voiceModelId") or "amazon.nova-2-sonic-v1:0"
        ).strip()
        vapid_public_key: str = (self.node.try_get_context("vapidPublicKey") or "").strip()
        # spec 028: the store reads the CURRENT public key from this SSM param
        # (kept current by the rotation Lambda) so it survives rotation without a
        # redeploy; vapidPublicKey above is the local-dev/bootstrap fallback env.
        vapid_public_key_param: str = (
            self.node.try_get_context("vapidPublicKeyParam") or "/restaurant-sim/web-push/vapid-public-key"
        ).strip()

        # spec 026: StoreStateCache last-known-state read fallback (AGENTPERF06
        # #3). An empty storeStateCacheTableName disables the feature and skips
        # the DynamoDB grant (gated-policy pattern); when set, the backend reads
        # the cache on a read timeout/transport error and writes-through reads.
        store_state_cache_table: str = (
            self.node.try_get_context("storeStateCacheTableName") or ""
        ).strip()
        tool_read_cache_enabled: str = "true" if store_state_cache_table else "false"

        # AGENTPERF06 #4: per-tool metrics namespace. Defaults to the namespace
        # the PutMetricData grant is scoped to (RestaurantSim/*), so deploying
        # turns the tool-layer telemetry on; set to "" to disable emission.
        tool_metrics_namespace: str = (
            self.node.try_get_context("toolMetricsNamespace") or "RestaurantSim/AgentTools"
        ).strip()

        # The voice broker streams to Nova Sonic from the EC2 role. Nova Sonic
        # bidi inference fans out CROSS-REGION and the service authz checks all
        # three invoke actions: the bare bidi action opens the stream, but the
        # inference is then denied with an empty-message AccessDeniedException on
        # the response stream unless InvokeModelWithResponseStream + InvokeModel
        # are also allowed. Verified on the deployed EC2 role (spec 025 Task 1f).
        # Scoped to the Nova Sonic foundation-model ARN (region wildcard for the
        # cross-region fan-out); no bedrock:*, no Resource:*, no inference-profile
        # (the bare model ARN suffices). voiceModelId MUST match the broker's
        # SONIC_MODEL_ID (architecture-decisions: pin model to role scope).
        self.ec2_role.attach_inline_policy(
            iam.Policy(
                self,
                "InvokeBedrockVoice",
                statements=[
                    iam.PolicyStatement(
                        actions=[
                            "bedrock:InvokeModelWithBidirectionalStream",
                            "bedrock:InvokeModelWithResponseStream",
                            "bedrock:InvokeModel",
                        ],
                        resources=[
                            f"arn:aws:bedrock:*::foundation-model/{voice_model_id}"
                        ],
                    ),
                ],
            ),
        )

        # spec 028: the store reads the current VAPID PUBLIC key from SSM (kept
        # current by the rotation Lambda). Scoped to that one parameter ARN --
        # the EC2 is NEVER granted read on the VAPID secret (private key stays
        # cloud-only). Gated on a non-empty param name.
        if vapid_public_key_param:
            self.ec2_role.attach_inline_policy(
                iam.Policy(
                    self,
                    "ReadVapidPublicKeyParam",
                    statements=[
                        iam.PolicyStatement(
                            sid="SsmGetVapidPublicKey",
                            actions=["ssm:GetParameter"],
                            resources=[
                                f"arn:aws:ssm:{self.region}:{self.account}:parameter{vapid_public_key_param}"
                            ],
                        ),
                    ],
                ),
            )

        # spec 026: scoped DynamoDB access for the read-cache fallback. Gated on
        # the table context (empty -> no grant); scoped to exactly the
        # StoreStateCache table ARN (no wildcard, no other table).
        if store_state_cache_table:
            self.ec2_role.attach_inline_policy(
                iam.Policy(
                    self,
                    "ReadWriteStoreStateCache",
                    statements=[
                        iam.PolicyStatement(
                            sid="DynamoToolReadCache",
                            actions=["dynamodb:GetItem", "dynamodb:PutItem"],
                            resources=[
                                f"arn:aws:dynamodb:{self.region}:{self.account}:table/{store_state_cache_table}"
                            ],
                        ),
                    ],
                ),
            )

        # AGENTPERF06 #4: CloudWatch dashboard + aggregate alarm for the
        # per-tool metrics emitted by the tool layer. Created when a metrics
        # namespace is configured (the default). SEARCH expressions render
        # every per-tool series without enumerating the tool set.
        if tool_metrics_namespace:
            _ns = tool_metrics_namespace
            _latency = cloudwatch.MathExpression(
                expression=f"SEARCH('{{{_ns},Tool}} MetricName=\"ToolLatencyMs\"', 'Average', 300)",
                label="avg ms",
                period=cdk.Duration.minutes(5),
            )
            _invocations = cloudwatch.MathExpression(
                expression=f"SEARCH('{{{_ns},Tool,Outcome}} MetricName=\"ToolInvocations\"', 'Sum', 300)",
                label="invocations",
                period=cdk.Duration.minutes(5),
            )
            _timeouts_total = cloudwatch.MathExpression(
                expression=f"SUM(SEARCH('{{{_ns},Tool}} MetricName=\"ToolTimeouts\"', 'Sum', 300))",
                label="timeouts (all tools)",
                period=cdk.Duration.minutes(5),
            )
            dashboard = cloudwatch.Dashboard(
                self,
                "AgentToolsDashboard",
                dashboard_name="RestaurantSim-AgentTools",
            )
            dashboard.add_widgets(
                cloudwatch.GraphWidget(
                    title="Tool latency (avg ms, per tool)", left=[_latency], width=12
                ),
                cloudwatch.GraphWidget(
                    title="Invocations by tool / outcome", left=[_invocations], width=12
                ),
                cloudwatch.GraphWidget(
                    title="Timeouts (all tools)", left=[_timeouts_total], width=12
                ),
            )
            # AGENTPERF06 #4 aggregate-timeout alarm on the dimensionless
            # ToolTimeoutsTotal metric (emitted by harness_tools on a timeout).
            # A plain Metric Alarm -- NOT a SEARCH expression: CloudWatch Metric
            # Alarms reject SEARCH() (dashboard-only), which is why the earlier
            # SUM(SEARCH(...)) version failed at CREATE and was replaced here
            # (2026-07-06). The dashboard "Timeouts (all tools)" widget above keeps
            # the per-tool SEARCH view.
            cloudwatch.Alarm(
                self,
                "AgentToolsHighTimeouts",
                alarm_name="RestaurantSim-AgentTools-HighTimeouts",
                metric=cloudwatch.Metric(
                    namespace=_ns,
                    metric_name="ToolTimeoutsTotal",
                    statistic="Sum",
                    period=cdk.Duration.minutes(5),
                ),
                threshold=10,
                evaluation_periods=3,
                datapoints_to_alarm=3,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description=(
                    "Agent tool timeouts across all tools exceeded the threshold "
                    "(AGENTPERF06 #4). Sum of the dimensionless ToolTimeoutsTotal "
                    "metric; starting threshold -- tune from observed rates."
                ),
            )

        user_data_script_path = _SCRIPTS_DIR / "user_data.sh"
        user_data_text = user_data_script_path.read_text(encoding="utf-8")
        user_data_text = (
            user_data_text
            .replace(
                "__REPO_S3_URI__",
                f"s3://{self.repo_asset.s3_bucket_name}/{self.repo_asset.s3_object_key}",
            )
            .replace("__IOT_CERT_SECRET_ID__", iot_cert_secret_id)
            .replace("__REGION__", self.region)
            # Spec 015 follow-up: render the chat-feature env vars into
            # /etc/restaurant-sim/env so the deployed FastAPI backend
            # can reach AgentCore Memory on first boot. ``memory_id`` is
            # read from CDK context earlier in the constructor; an
            # empty string is a valid render target (the backend logs a
            # "not configured" warning rather than failing to start).
            .replace("__AGENTCORE_MEMORY_ID__", memory_id)
            # Spec 022: harness wiring. The harness is the only chat backend
            # (the ``agentBackend`` selector was removed in Task 6). harnessArn
            # is validated non-empty at synth time; iotEndpoint feeds the
            # harness inline-tool path.
            .replace("__HARNESS_ARN__", harness_arn)
            .replace("__IOT_ENDPOINT__", iot_endpoint)
            # spec 025: voice-edge flag + Web Push application-server public key.
            .replace("__VOICE_ENABLED__", voice_enabled)
            .replace("__VAPID_PUBLIC_KEY__", vapid_public_key)
            .replace("__VAPID_PUBLIC_KEY_PARAM__", vapid_public_key_param)
            # spec 026: read-cache fallback config (empty table => disabled).
            .replace("__STORE_STATE_CACHE_TABLE__", store_state_cache_table)
            .replace("__TOOL_READ_CACHE_ENABLED__", tool_read_cache_enabled)
            # AGENTPERF06 #4: per-tool metrics namespace (empty => emission off).
            .replace("__TOOL_METRICS_NAMESPACE__", tool_metrics_namespace)
        )
        self.user_data = ec2.UserData.custom(user_data_text)

        # --------------------------------------------------------------
        # Task 7.1: resolve the latest Amazon Linux 2023 ARM64 AMI via
        # the SSM public parameter. Using ``from_ssm_parameter`` means
        # the AMI ID is resolved by CloudFormation at deploy time, so
        # re-deploys pick up patched images automatically (R4.3).
        # --------------------------------------------------------------
        machine_image = ec2.MachineImage.from_ssm_parameter(
            "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64",
            os=ec2.OperatingSystemType.LINUX,
        )

        # --------------------------------------------------------------
        # Task 7.2: the single EC2 instance that runs the simulator.
        # --------------------------------------------------------------
        #
        # ``t4g.small`` is ARM64 (Graviton) with 2 vCPU / 2 GiB RAM —
        # enough headroom for the FastAPI process plus the Vite build
        # that user-data runs on first boot (R4.2).
        #
        # The instance is pinned to one AZ (the first the VPC reports)
        # so we end up with exactly one ENI in exactly one subnet — the
        # design calls for a single instance (R4.1). The VPC still
        # spans two AZs so the ALB can be multi-AZ; that's orthogonal.
        #
        # No ``key_name`` / ``key_pair`` argument is supplied, which
        # omits ``KeyName`` from the generated CloudFormation (R4.7).
        # Private subnets don't map a public IP, satisfying R4.6.
        #
        # ``user_data_causes_replacement=True`` (default False) is load-
        # bearing: the UserData script is what installs /opt/restaurant-
        # sim, builds the frontend, and writes /etc/restaurant-sim/env.
        # It is cloud-init ``per-instance``, so it runs once per unique
        # instance id and NOT on a stop/start. Without this flag, a
        # ``cdk deploy`` that changes UserData (new repo-asset hash, new
        # env-var values, new systemd unit) applies the change as a CFN
        # "Some interruptions" metadata-only update: the instance is
        # stopped, its launch metadata is rewritten, and it is started
        # again — but nothing inside /opt/restaurant-sim or /etc/
        # restaurant-sim is refreshed, because cloud-init sees the sem
        # file under /var/lib/cloud/instance/sem/ and skips the script.
        # Net effect: every apparent "UserData deploy" silently drifts
        # further from the repo. See the 2026-05-05 amendment to
        # the project architecture-decisions notes,
        # "EC2 user-data bundles and stale deployment state" for the
        # full diagnosis. With this flag on, CDK folds a UserData hash
        # into the instance logical id, so any UserData change becomes
        # a CFN replacement and the fresh instance goes through the
        # bootstrap from scratch. The tradeoff — cosmetic comment
        # changes also force replacement — is acceptable for a
        # bootstrap-from-asset deploy model.
        self.ec2_instance = ec2.Instance(
            self,
            "SimulatorInstance",
            instance_type=ec2.InstanceType("t4g.small"),
            # 1-minute metrics (cdk-nag AwsSolutions-EC28). Cheap, and this is a
            # single long-lived instance whose health the ALB depends on.
            detailed_monitoring=True,
            machine_image=machine_image,
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                availability_zones=[self.vpc.availability_zones[0]],
            ),
            security_group=self.ec2_sg,
            role=self.ec2_role,
            user_data=self.user_data,
            user_data_causes_replacement=True,
            block_devices=[
                # R4.5: encrypted gp3 root volume, 20 GiB.
                ec2.BlockDevice(
                    device_name="/dev/xvda",
                    volume=ec2.BlockDeviceVolume.ebs(
                        20,
                        encrypted=True,
                        volume_type=ec2.EbsDeviceVolumeType.GP3,
                    ),
                ),
            ],
        )

        # --------------------------------------------------------------
        # Spec 029, Task 3: the INTERNAL, HTTP-only Application Load Balancer.
        # --------------------------------------------------------------
        #
        # The ALB moves to the VPC's PRIVATE (with-egress) subnets and is no
        # longer internet-facing: CloudFront reaches it through a VPC origin
        # (below), so it needs no public IP. This structurally removes the
        # spec-007 "bypass auth by hitting the ALB directly" weakness.
        # ``idle_timeout=3600s`` keeps long-lived WebSocket (``/ws``)
        # connections alive (R6.2). Spec 007's Route 53 hosted zone, ACM
        # certificate, and A-record alias are removed (Task 7) - trusted TLS
        # now terminates at CloudFront's default ``*.cloudfront.net`` cert.
        self.alb = elbv2.ApplicationLoadBalancer(
            self,
            "SimulatorAlb",
            vpc=self.vpc,
            internet_facing=False,
            security_group=self.alb_sg,
            idle_timeout=cdk.Duration.seconds(3600),
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
            ),
        )

        # --------------------------------------------------------------
        # Spec 029, Tasks 4 & 8: CloudFront distribution (VPC origin) with a
        # gated Lambda@Edge (Cognito@Edge) viewer-request authenticator.
        # --------------------------------------------------------------
        #
        # Decision D3-A (synth-time config injection, two-phase bootstrap):
        # Lambda@Edge forbids environment variables, so the Cognito IDs and the
        # CSRF nonce-signing secret are read from CDK context and written into
        # the edge function's bundled config AT SYNTH. On the first (bootstrap)
        # deploy the ``edgeCognito*`` context is empty, so the edge function is
        # NOT created or associated and the distribution comes up without auth;
        # after capturing the Cognito IDs into context, a second deploy bakes
        # them in and associates the authenticator.
        #
        # Per ARCC SAX-02 (secrets management) the injected config is written to
        # a GITIGNORED sidecar (``config.local.json``) that the handler prefers
        # over the committed empty ``config.json`` placeholder, so the real CSRF
        # secret never lands in a git-tracked file.
        edge_auth_dir = Path(__file__).resolve().parents[1] / "lambdas" / "edge-auth"
        # The sidecar is normally written into the real edge-auth asset dir so
        # ``Code.from_asset`` bundles it. Tests set ``edgeConfigDirOverride`` to a
        # throwaway dir so a test synth never clobbers the real, secret-bearing
        # config.local.json (the Lambda asset always bundles ``edge_auth_dir``).
        edge_config_dir = Path(
            self.node.try_get_context("edgeConfigDirOverride") or edge_auth_dir
        )
        edge_user_pool_id = self.node.try_get_context("edgeCognitoUserPoolId") or ""
        edge_app_client_id = self.node.try_get_context("edgeCognitoAppClientId") or ""
        edge_user_pool_domain = self.node.try_get_context("edgeCognitoUserPoolDomain") or ""
        edge_csrf_nonce_secret = self.node.try_get_context("edgeCsrfNonceSecret") or ""

        # Gate creation + association on the phase-2 app client id being set.
        # The three NON-SECRET ``edgeCognito*`` IDs are account-specific, so
        # they live in the gitignored cdk.context.json rather than the committed
        # cdk.json; ``edgeAuthRequired`` (default true) makes their absence a
        # loud synth failure instead of silently deploying an UNAUTHENTICATED
        # distribution -- the "empty-default gate silently disables the
        # resource" trap documented in architecture-decisions.md.
        edge_auth_enabled = bool(edge_app_client_id)
        if _flag_is_set(self.node, EDGE_AUTH_REQUIRED_FLAG) and not edge_auth_enabled:
            raise ValueError(
                "SimulatorEc2AlbStack is being synthesized with "
                f"'{EDGE_AUTH_REQUIRED_FLAG}' set (the default), but the CDK "
                "context key 'edgeCognitoAppClientId' is empty. Without it the "
                "Lambda@Edge Cognito authenticator is not associated and the "
                "CloudFront distribution would serve the dashboard with NO "
                "authentication. Set the edgeCognito* values in the gitignored "
                "cdk.context.json (or pass them with -c). During the one-time "
                "two-phase bootstrap -- where the Cognito user pool does not "
                "exist yet -- opt out explicitly with "
                f"`-c {EDGE_AUTH_REQUIRED_FLAG}=false`."
            )

        edge_lambdas = None
        if edge_auth_enabled:
            sidecar_path = edge_config_dir / "config.local.json"

            # CSRF nonce-signing secret resolution (spec 029 Option A): prefer an
            # explicit ``-c edgeCsrfNonceSecret`` (first bootstrap or rotation);
            # otherwise reuse the secret already persisted in the gitignored
            # sidecar so routine deploys need no ``-c``. The secret stays out of
            # git (ARCC SAX-02). Never bake an empty secret -- fail loud so a
            # misconfigured deploy cannot silently break CSRF / auth.
            if not edge_csrf_nonce_secret and sidecar_path.exists():
                try:
                    edge_csrf_nonce_secret = json.loads(
                        sidecar_path.read_text(encoding="utf-8")
                    ).get("csrfNonceSecret", "")
                except (json.JSONDecodeError, OSError):
                    pass  # still empty from the context lookup; the check below fails loud
            if not edge_csrf_nonce_secret:
                raise ValueError(
                    "Edge auth is enabled (edgeCognitoAppClientId is set) but no "
                    "CSRF nonce secret is available. Pass "
                    "-c edgeCsrfNonceSecret=$(openssl rand -hex 32) on the first "
                    "edge-auth deploy; later deploys reuse it from the gitignored "
                    f"{sidecar_path.name} sidecar."
                )

            sidecar_path.write_text(
                json.dumps(
                    {
                        "region": self.region,  # Cognito user pool region
                        "userPoolId": edge_user_pool_id,
                        "userPoolAppId": edge_app_client_id,
                        "userPoolDomain": edge_user_pool_domain,
                        "csrfNonceSecret": edge_csrf_nonce_secret,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            self.edge_auth_fn = cloudfront.experimental.EdgeFunction(
                self,
                "EdgeAuthFn",
                runtime=lambda_.Runtime.NODEJS_20_X,
                handler="index.handler",
                code=lambda_.Code.from_asset(str(edge_auth_dir)),
            )
            edge_lambdas = [
                cloudfront.EdgeLambda(
                    function_version=self.edge_auth_fn.current_version,
                    event_type=cloudfront.LambdaEdgeEventType.VIEWER_REQUEST,
                ),
            ]

        # VPC origin to the internal ALB over HTTP:80. Decision D1-A + the
        # accepted non-prod risk on Shepherd ``CloudFrontTLSIsUsedToOrigin``
        # (see design.md "Security reconciliation"): an HTTPS origin would need
        # a browser-trusted cert on the internal ALB, reintroducing the
        # domain/ACM dependency this spec removes; VPC Origins keeps the
        # CloudFront -> ALB hop on the private AWS network path.
        alb_vpc_origin = origins.VpcOrigin.with_application_load_balancer(
            self.alb,
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
            http_port=80,
        )

        self.distribution = cloudfront.Distribution(
            self,
            "SimulatorDistribution",
            comment="Restaurant Simulator - CloudFront edge auth (spec 029)",
            default_behavior=cloudfront.BehaviorOptions(
                origin=alb_vpc_origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER,
                edge_lambdas=edge_lambdas,
            ),
        )

        # --------------------------------------------------------------
        # Spec 029, Task 5: lock the internal ALB to CloudFront's VPC origin.
        # --------------------------------------------------------------
        #
        # CloudFront provisions a managed "CloudFront-VPCOrigins-Service-SG"
        # security group in the VPC for VPC-origin traffic. It does not exist
        # until the distribution's VPC origin is created, so a read-only EC2
        # DescribeSecurityGroups custom resource (ordered after the
        # distribution) resolves its id, and only HTTP:80 from it is allowed to
        # the ALB. There is no ``0.0.0.0/0`` ingress on the ALB (R2.3, R7).
        vpc_origins_sg_lookup = cr.AwsCustomResource(
            self,
            "VpcOriginsSgLookup",
            on_update=cr.AwsSdkCall(
                service="EC2",
                action="describeSecurityGroups",
                parameters={
                    "Filters": [
                        {"Name": "vpc-id", "Values": [self.vpc.vpc_id]},
                        {
                            "Name": "group-name",
                            "Values": ["CloudFront-VPCOrigins-Service-SG"],
                        },
                    ],
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    "CloudFront-VPCOrigins-Service-SG-lookup",
                ),
            ),
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
                resources=cr.AwsCustomResourcePolicy.ANY_RESOURCE,
            ),
        )
        vpc_origins_sg_lookup.node.add_dependency(self.distribution)

        vpc_origins_sg = ec2.SecurityGroup.from_security_group_id(
            self,
            "CloudFrontVpcOriginsSg",
            vpc_origins_sg_lookup.get_response_field("SecurityGroups.0.GroupId"),
            mutable=False,
        )
        self.alb.connections.allow_from(
            vpc_origins_sg,
            ec2.Port.tcp(80),
            "HTTP from CloudFront VPC origin service SG",
        )

        # --------------------------------------------------------------
        # Spec 007, Task 2: Cognito User Pool, App Client, Hosted UI domain.
        # --------------------------------------------------------------

        # R1.1–R1.6: Cognito User Pool with email sign-in, self-signup,
        # email verification, and DESTROY removal policy.
        self.user_pool = cognito.UserPool(
            self,
            "SimulatorUserPool",
            user_pool_name="restaurant-sim-users",
            self_sign_up_enabled=True,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            # Password policy (cdk-nag AwsSolutions-COG1) and optional MFA
            # (AwsSolutions-COG2). Fixed rather than suppressed: this pool is the
            # ONLY thing standing between the public internet and the dashboard
            # (the ALB is internal, so edge auth is the whole perimeter).
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
            ),
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(sms=False, otp=True),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # R4.1-R4.5 (Task 6): PUBLIC app client (decision D2-A - no secret) for
        # the Cognito@Edge OAuth2 authorization-code flow. Callback/logout URLs
        # target the CloudFront distribution's default ``*.cloudfront.net``
        # domain (no custom domain). ``cognito-at-edge`` runs the flow at the
        # edge; the ALB no longer performs authentication.
        self.user_pool_client = self.user_pool.add_client(
            "SimulatorAppClient",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(
                user_password=True,
                user_srp=True,
            ),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID],
                callback_urls=[
                    f"https://{self.distribution.distribution_domain_name}/oauth2/idpresponse",
                ],
                logout_urls=[
                    f"https://{self.distribution.distribution_domain_name}/",
                ],
            ),
            supported_identity_providers=[
                cognito.UserPoolClientIdentityProvider.COGNITO,
            ],
        )

        # R3.1–R3.2: Cognito Hosted UI domain. The prefix must be globally
        # unique; default to restaurant-sim-{account ID}. The account ID
        # is a CloudFormation token at synth time, so we use Fn.join to
        # build the prefix string.
        cognito_prefix_ctx = self.node.try_get_context("cognito_domain_prefix")
        if cognito_prefix_ctx:
            cognito_prefix = cognito_prefix_ctx
        else:
            cognito_prefix = cdk.Fn.join("", [
                "restaurant-sim-",
                cdk.Aws.ACCOUNT_ID,
            ])

        self.user_pool_domain = self.user_pool.add_domain(
            "SimulatorCognitoDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=cognito_prefix,
            ),
        )

        # --------------------------------------------------------------
        # Spec 029, Task 3: target group + single HTTP:80 listener.
        # --------------------------------------------------------------
        #
        # The target group serves HTTP:8000 on the EC2 (``/api/health`` health
        # check, 1-day stickiness). Auth + TLS moved to CloudFront / the edge,
        # so the ALB exposes a single plain HTTP:80 listener that forwards here.
        #
        # NOTE (spec 029, live-deploy fix): the construct id intentionally
        # differs from spec 007's ``SimulatorTargetGroup``. The spec-029 ALB
        # scheme change (internet-facing -> internal) forces an ALB
        # *replacement*, and ELB forbids a target group being attached to two
        # load balancers at once. If this TG kept its old logical id,
        # CloudFormation's create-then-delete replacement ordering would attach
        # the retained TG to the new ALB's listener while it was still attached
        # to the old ALB's listener, failing with "target groups cannot be
        # associated with more than one load balancer". Giving the TG a new
        # logical id makes CFN create a fresh TG for the new ALB and tear the
        # old TG down with the old ALB, so the two never overlap.
        self.target_group = elbv2.ApplicationTargetGroup(
            self,
            "SimulatorHttpTargetGroup",
            vpc=self.vpc,
            protocol=elbv2.ApplicationProtocol.HTTP,
            port=8000,
            targets=[elbv2_targets.InstanceTarget(self.ec2_instance, port=8000)],
            health_check=elbv2.HealthCheck(
                path="/api/health",
                protocol=elbv2.Protocol.HTTP,
                interval=cdk.Duration.seconds(30),
                timeout=cdk.Duration.seconds(5),
                healthy_threshold_count=2,
                unhealthy_threshold_count=2,
            ),
            stickiness_cookie_duration=cdk.Duration.days(1),
        )

        self.http_listener = self.alb.add_listener(
            "HttpListener",
            port=80,
            protocol=elbv2.ApplicationProtocol.HTTP,
            open=False,
            default_action=elbv2.ListenerAction.forward([self.target_group]),
        )

        # --------------------------------------------------------------
        # Spec 029, Task 9: stack outputs.
        # --------------------------------------------------------------
        #
        # These outputs are consumed by human operators (the CloudFront URL for
        # the dashboard, instance id for ``aws ssm start-session``) and by spec
        # 005 (VPC id, EC2 SG id, MQTT secret ARN to wire up the MQTT broker
        # stack). The CfnOutput construct id is used verbatim as the
        # CloudFormation ``Outputs`` logical key - no hash suffix. The internal
        # ALB is intentionally not surfaced as an output (CloudFront is the
        # public entry point).
        # ------------------------------------------------------------------
        # Stack-level suppressions for four rule families whose justification is
        # the SAME across every occurrence. Stack scope is deliberate and is the
        # broadest suppression in this repo -- per-resource entries were tried
        # first, but these 23 findings sit on 10+ constructs (six named inline
        # policies, the EC2 role and its DefaultPolicy, two CDK-generated custom
        # resources, and the cross-region Lambda@Edge support stack) with only
        # two distinct reasons between them, so per-resource copies would be
        # noise that obscures which reason applies.
        #
        # The families are scoped tightly enough that this does not become a
        # blanket exemption: nothing here suppresses a data-store, network,
        # encryption or authentication rule -- those were fixed (VPC7, EC28,
        # COG1) or answered individually above (CFR*, COG*, ELB2, EC29, SMG4).
        # ------------------------------------------------------------------
        NagSuppressions.add_stack_suppressions(
            self,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "Every wildcard on this stack's roles is a runtime-value "
                        "wildcard, not a resource-class wildcard. The recurring cases: "
                        "(a) iot topic ARNs wildcard the STORE ID and correlation id "
                        "(stores/*/commands/{domain}, stores/*/responses/*) -- the "
                        "cloud->store half of the per-store isolation model, where "
                        "pinning ids would mean redeploying per store onboarded; "
                        "(b) the harness ARN uses a name-PREFIX wildcard so the role "
                        "survives re-provisioning (see architecture-decisions.md: "
                        "invoke_harness needs both InvokeHarness and "
                        "InvokeAgentRuntime, scoped to harness/<name>-*); "
                        "(c) bedrock foundation-model ARNs use a REGION wildcard "
                        "because Nova Sonic inference fans out cross-region; "
                        "(d) cloudwatch:PutMetricData and the AgentCore Memory "
                        "child-record path take no narrower resource; and "
                        "(e) CDK-generated custom resources (VpcOriginsSgLookup, the "
                        "log-retention provider) carry scopes set by aws-cdk-lib. None "
                        "grants access to another tenant's data or another service's "
                        "resources."
                    ),
                },
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": (
                        "AWSLambdaBasicExecutionRole on CDK-generated helper functions "
                        "(the custom-resource provider and the Lambda@Edge "
                        "authenticator's role in the cross-region support stack), plus "
                        "the EC2 role's SSM managed policy which is what makes "
                        "`aws ssm start-session` work in place of SSH -- the reason "
                        "this VPC needs no bastion and no inbound port."
                    ),
                },
                {
                    "id": "AwsSolutions-L1",
                    "reason": (
                        "Both non-latest runtimes belong to CDK-generated functions, "
                        "not to application code: the custom-resource provider that "
                        "aws-cdk-lib synthesizes, and the Lambda@Edge authenticator "
                        "whose runtime is additionally constrained by Lambda@Edge "
                        "itself (edge functions support a narrower runtime set than "
                        "regional Lambda). Neither runtime is chosen by this stack."
                    ),
                },
                {
                    "id": "CdkNagValidationFailure",
                    "reason": (
                        "AwsSolutions-EC23 cannot evaluate the security-group ingress "
                        "because the CIDR is an unresolved intrinsic "
                        "(Fn::GetAtt on the VPC's own CidrBlock) rather than a literal. "
                        "Verified manually in the synthesized template: the ONLY CIDR "
                        "ingress in this stack is port 443 from the VPC's own CIDR "
                        "block, for the interface VPC endpoint. There is no "
                        "0.0.0.0/0 ingress anywhere -- the ALB is internal and "
                        "CloudFront reaches it through a VPC origin."
                    ),
                },
            ],
        )

        # ==================================================================
        # cdk-nag suppressions (P1). This stack is the DEV/TEST simulator
        # harness -- the README states it is not part of the production
        # solution -- which is the backdrop for several acceptances below.
        #
        # FIXED rather than suppressed: VPC7 (flow logs), EC28 (detailed
        # monitoring), COG1 (password policy).
        # ==================================================================
        NagSuppressions.add_resource_suppressions(
            self.distribution,
            [
                {
                    "id": "AwsSolutions-CFR1",
                    "reason": (
                        "No geo restriction. Access is gated by Cognito at the edge, "
                        "not by viewer country, and there is no jurisdictional "
                        "requirement for a non-production harness."
                    ),
                },
                {
                    "id": "AwsSolutions-CFR2",
                    "reason": (
                        "No AWS WAF. The origin is an INTERNAL ALB reachable only via "
                        "this distribution, every request is authenticated at the edge "
                        "before it reaches the origin, and there is no anonymous "
                        "surface to filter. A web ACL would add recurring cost for a "
                        "dev harness with no untrusted traffic."
                    ),
                },
                {
                    "id": "AwsSolutions-CFR3",
                    "reason": (
                        "No CloudFront access logging to S3. Request-level visibility "
                        "already exists one hop later: the CloudWatch agent ships "
                        "uvicorn access logs to the /simulator/app log group, and the "
                        "Lambda@Edge authenticator logs its own decisions. S3 "
                        "distribution logs would add a bucket plus lifecycle "
                        "management for data already captured."
                    ),
                },
                {
                    "id": "AwsSolutions-CFR4",
                    "reason": (
                        "Cannot enforce a TLSv1.2+ minimum on this distribution. Spec "
                        "029 deliberately uses the free default *.cloudfront.net "
                        "certificate (no domain, no Route 53, no ACM), and with the "
                        "default certificate CloudFront controls the security policy "
                        "and rejects a minimum-protocol override. Enforcing TLS 1.2+ "
                        "would require buying a domain and an ACM certificate, which "
                        "spec 029 explicitly scoped out."
                    ),
                },
            ],
        )

        NagSuppressions.add_resource_suppressions(
            self.user_pool,
            [
                {
                    "id": "AwsSolutions-COG2",
                    "reason": (
                        "MFA is OPTIONAL with TOTP rather than REQUIRED. This pool "
                        "exists so a single operator can reach a demo dashboard, and "
                        "REQUIRED MFA forces every fresh demo login through TOTP "
                        "enrolment. The password policy IS enforced (12 chars, all "
                        "four character classes) -- COG1 was fixed, not suppressed. "
                        "Open self-registration (self_sign_up_enabled=True) is a "
                        "CONSCIOUS decision for this demo, confirmed 2026-08-07: the "
                        "dashboard shows simulated store data only, so open signup is "
                        "accepted to keep the demo reachable without operator "
                        "provisioning. It follows that MFA strength is not the "
                        "control here -- do not 'harden' the MFA mode in isolation and "
                        "conclude the pool is locked down. A deployment carrying real "
                        "store data would need BOTH decisions revisited together."
                    ),
                },
                {
                    "id": "AwsSolutions-COG8",
                    "reason": (
                        "Advanced Security Mode (adaptive auth, compromised-credential "
                        "detection) is a paid Cognito feature. Not enabled for a "
                        "non-production harness holding no customer data."
                    ),
                },
            ],
        )

        NagSuppressions.add_resource_suppressions(
            self.alb,
            [
                {
                    "id": "AwsSolutions-ELB2",
                    "reason": (
                        "No ALB access logs to S3. This ALB is INTERNAL and its only "
                        "client is the CloudFront VPC origin, so its access log would "
                        "duplicate the origin-side uvicorn access log already shipped "
                        "to CloudWatch. Enabling it means a bucket and lifecycle "
                        "policy for redundant data."
                    ),
                }
            ],
        )

        NagSuppressions.add_resource_suppressions(
            self.ec2_instance,
            [
                {
                    "id": "AwsSolutions-EC29",
                    "reason": (
                        "Termination protection is deliberately OFF and the instance is "
                        "deliberately not in an ASG. The construct sets "
                        "user_data_causes_replacement=True, so every user-data or "
                        "repo-asset change REPLACES the instance -- that is the deploy "
                        "model (architecture-decisions.md 'EC2 user-data bundles and "
                        "stale deployment state' documents the silent stale-deploy bug "
                        "caused by the flag's absence). Termination protection would "
                        "block that replacement and break deploys. The instance holds "
                        "no durable state; it bootstraps from the repo asset on boot."
                    ),
                }
            ],
        )

        # NOTE (2026-09-13): the ``AwsSolutions-SMG4`` suppression that used to sit here
        # is gone along with the resource it excused. It argued that the Amazon MQ
        # placeholder secret needed no rotation because it was empty and unread -- which
        # was true, and was the argument for DELETING it rather than suppressing a
        # finding about it. Deleting the secret removes the finding at its source; a
        # suppression left behind after the fix only hides the next drift.

        CfnOutput(
            self,
            "CloudFrontDomainName",
            value=self.distribution.distribution_domain_name,
            description="CloudFront distribution domain - the public entry point.",
        )
        CfnOutput(
            self,
            "CloudFrontUrl",
            value=f"https://{self.distribution.distribution_domain_name}/",
            description="Full CloudFront URL for the simulator dashboard.",
        )
        CfnOutput(
            self,
            "InstanceId",
            value=self.ec2_instance.instance_id,
            description="EC2 instance ID (use for `aws ssm start-session`).",
        )
        CfnOutput(
            self,
            "Region",
            value=self.region,
            description="AWS region of this stack.",
        )
        CfnOutput(
            self,
            "VpcId",
            value=self.vpc.vpc_id,
            description="VPC ID (consumed by spec 005).",
        )
        CfnOutput(
            self,
            "Ec2SecurityGroupId",
            value=self.ec2_sg.security_group_id,
            description="EC2 security group ID (consumed by spec 005).",
        )
        # R10.6 (removed 2026-09-13): the ``MqttSecretArn`` output is gone with the
        # secret it exported. Its only consumer was the spec 005 Amazon MQ broker,
        # decommissioned in spec 020, and this stack's outputs never set
        # ``export_name``, so no stack could have imported it.

        # --------------------------------------------------------------
        # Spec 007, Task 5: Cognito stack outputs.
        # --------------------------------------------------------------
        CfnOutput(
            self,
            "CognitoUserPoolId",
            value=self.user_pool.user_pool_id,
            description="Cognito User Pool ID for the simulator.",
        )
        CfnOutput(
            self,
            "CognitoAppClientId",
            value=self.user_pool_client.user_pool_client_id,
            description="Cognito App Client ID for the Cognito@Edge OAuth2 flow.",
        )
