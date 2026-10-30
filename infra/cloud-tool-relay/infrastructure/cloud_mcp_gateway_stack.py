# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CDK stack for the cloud-side MCP gateway + Bedrock AgentCore agents.

Per spec 006, this stack is pinned to AWS region ``us-west-2`` (requirements
R1.1 and R1.3) and applies a uniform set of tags to every resource it creates
(requirement R1.4).

This file — as checked in for **Task 1** of the implementation plan — only
establishes the scaffold:

* Region lock (R1.1 / R1.3) via ``REQUIRED_REGION``.
* Stack-level tagging (R1.4) via ``STACK_TAGS``.
* Consumption of spec 005's four stack exports (R2.1 / R2.2) surfaced as
  typed stack attributes for later tasks to reuse.

All of the actual resources (DynamoDB, Lambdas, Fargate, ECR, AgentCore,
outputs, IAM) are added in later tasks and are deliberately not present yet.
The pattern mirrors the other CDK stacks under ``infra/`` (originally
``infra/mqtt-broker``, deleted when Amazon MQ was retired -- see ADR-004).
"""
from __future__ import annotations

from typing import Final

import aws_cdk as cdk
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda
from aws_cdk import aws_logs as logs
from cdk_nag import NagSuppressions
from constructs import Construct

#: Region this stack is allowed to deploy into. Spec 006 (R1.1, R1.3).
REQUIRED_REGION: Final[str] = "us-west-2"

#: Tags applied to every resource in the stack. Spec 006 (R1.4).
STACK_TAGS: Final[dict[str, str]] = {
    "Project": "restaurant-simulator",
    "Environment": "dev",
    "ManagedBy": "cdk",
    "Spec": "006-cloud-mcp-gateway-cdk",
}


class CloudMcpGatewayStack(cdk.Stack):
    """Infrastructure stack for the cloud-side MCP gateway + agents.

    The stack is region-locked to ``us-west-2``. Attempting to instantiate it
    against any other region raises :class:`ValueError` at synth time, well
    before CloudFormation is ever contacted.

    Commands are published to AWS IoT Core via the connection-less
    ``iot-data:Publish`` data plane. The stack therefore consumes no exports
    from spec 005's ``MqttBrokerStack`` — Amazon MQ was decommissioned in
    spec 020 Task 8.
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
                "CloudMcpGatewayStack must be deployed in "
                f"{REQUIRED_REGION!r}, got {self.region!r}"
            )

        # R1.4: apply the standard tag set to every resource in the stack.
        # ``Tags.of(self)`` propagates tags to every taggable construct added
        # to the stack, including those introduced by later tasks.
        tags = cdk.Tags.of(self)
        for key, value in STACK_TAGS.items():
            tags.add(key, value)

        # --------------------------------------------------------------
        # Amazon MQ decommissioned (spec 020 Task 8)
        # --------------------------------------------------------------
        #
        # The gateway used to import four exports from spec 005's
        # ``MqttBrokerStack`` (``BrokerEndpoint``, ``BrokerPort``,
        # ``MasterCredentialsSecretArn``, ``Region``) and publish commands to
        # Amazon MQ over MQTT. Spec 020 migrated the transport to AWS IoT Core
        # (connection-less ``iot-data:Publish``), and Task 8 decommissioned the
        # broker. The ``Fn.import_value`` calls were removed both because the
        # values are no longer used and because CloudFormation refuses to delete
        # a stack whose exports are still imported — keeping them would block
        # ``cdk destroy`` of ``MqttBrokerStack``.

        # --------------------------------------------------------------
        # Task 2 — DynamoDB tables (R3.1, R3.2, R3.3)
        # --------------------------------------------------------------
        #
        # The stack owns two DynamoDB tables:
        #
        # * ``CommandResults`` — per-invocation correlation store. Gateway
        #   Lambdas PutItem a ``pending`` record keyed by a freshly-minted
        #   correlation UUID; the Fargate response handler (task 5)
        #   UpdateItem's the same record to ``complete`` when the store
        #   replies over MQTT; domain agents (task 8) poll GetItem for
        #   status. Records self-clean via the ``expires_at`` TTL attribute
        #   so abandoned pendings don't accumulate.
        # * ``StoreStateCache`` — last-known-good write-through cache per
        #   ``{store_id}#{domain}``. Added in task 2.2, not here.
        #
        # Task 2.1 builds only the ``CommandResults`` table; task 2.2 adds
        # ``StoreStateCache`` under this same banner; task 2.3 exposes the
        # names/ARNs as stack attributes for the Lambda / Fargate tasks
        # that consume them.

        # ``CommandResults`` — correlation-id keyed, TTL-bounded, on-demand
        # billed DynamoDB table. Each property below is a deliberate choice
        # traceable to requirements R3.1 and R3.3:
        #
        # * ``partition_key=correlation_id (STRING)``: the gateway Lambda
        #   generates a UUID v4 per tool invocation and that UUID is the
        #   only natural identifier the request/response pair shares. No
        #   sort key — there is at most one row per correlation id.
        # * ``billing_mode=PAY_PER_REQUEST``: dev traffic is bursty and
        #   low-volume; on-demand pricing removes capacity planning and is
        #   cheaper than the minimum provisioned throughput at this scale.
        # * ``time_to_live_attribute="expires_at"``: gateway Lambdas set
        #   ``expires_at = now + 300s`` on the pending record (application
        #   concern). Configuring TTL on the table itself means records
        #   from failed / dropped flows disappear on their own instead of
        #   growing forever — R3.1 calls this out explicitly.
        # * ``table_name="CommandResults"``: a deterministic physical name
        #   is required because task 11 emits ``CommandResultsTableName``
        #   as a CloudFormation output and downstream specs / smoke tests
        #   reference the name directly. CDK would otherwise append a
        #   hash-suffixed physical name.
        # * Encryption: omitted on purpose. R3.1 / R3.3 require SSE with
        #   the AWS-owned KMS key, which is DynamoDB's default and renders
        #   as *no* ``SSESpecification`` block in CloudFormation. Setting
        #   ``encryption=TableEncryption.DEFAULT`` in aws-cdk-lib 2.170 is
        #   equivalent; leaving the parameter off keeps the template
        #   minimal and matches the assertion in
        #   ``tests/test_dynamodb_tables.py::test_command_results_encryption_is_aws_owned``.
        # * Point-in-time recovery: not enabled. R3.1 calls this the dev
        #   default; CDK's default is already "disabled" so the parameter
        #   is deliberately omitted rather than set to ``False`` (setting
        #   it to ``False`` would emit an explicit ``PointInTimeRecoveryEnabled: false``
        #   into the template, which is harmless but noisier).
        # * ``removal_policy=DESTROY``: lets ``cdk destroy`` wipe the
        #   table cleanly in dev. In production this would be ``RETAIN``
        #   so an accidental stack delete does not eat correlation data.
        self.command_results_table: dynamodb.Table = dynamodb.Table(
            self,
            "CommandResultsTable",
            table_name="CommandResults",
            partition_key=dynamodb.Attribute(
                name="correlation_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",
            removal_policy=cdk.RemovalPolicy.DESTROY,  # production would use RETAIN
        )

        # ``StoreStateCache`` — last-known-good, write-through per-domain
        # cache that gives domain agents (task 8) a useful-but-stale answer
        # when a live round-trip over MQTT times out. Traces to requirements
        # R3.2 and R3.3. Each property below is a deliberate choice:
        #
        # * ``partition_key=store_domain_key (STRING)``: composite key of
        #   the form ``{store_id}#{domain}`` (design.md D4). One row per
        #   ``(store, domain)`` pair. Rationale for domain-level rather
        #   than per-tool granularity: the blog frames the cache as
        #   domain-scoped ("equipment temperatures, POS totals, inventory
        #   levels"), and at dev scale a single item per domain is
        #   sufficient for the fallback flow. Per-tool caching is a
        #   backward-compatible evolution: adding a ``tool_name`` sort
        #   key later would narrow the grain without rewriting the PK or
        #   invalidating existing items.
        # * ``billing_mode=PAY_PER_REQUEST``: same rationale as
        #   ``CommandResults`` — bursty low-volume dev traffic, no capacity
        #   planning, cheaper than the minimum provisioned throughput.
        # * **No TTL** — intentionally omitted. The whole point of this
        #   cache is to survive a store going offline for hours or days:
        #   an expiring entry would evaporate exactly when the fallback
        #   is most needed. Write-through on every successful read (see
        #   task 5's Fargate response handler) keeps the data fresh
        #   under normal operation; the absence of TTL protects against
        #   the degraded case.
        # * ``table_name="StoreStateCache"``: deterministic physical name
        #   so task 11 can emit ``StoreStateCacheTableName`` as a
        #   CloudFormation output and downstream consumers (the Fargate
        #   response handler, domain agents) can reference the table by
        #   name rather than via synth-time cross-construct refs.
        # * Encryption: parameter omitted — DynamoDB's default is the
        #   AWS-owned KMS key, which matches R3.2 and renders as no
        #   ``SSESpecification`` block in CloudFormation. Same choice
        #   as ``CommandResults`` above.
        # * Point-in-time recovery: not enabled. Dev default per R3.2;
        #   parameter deliberately omitted so the template stays minimal.
        # * ``removal_policy=DESTROY``: dev choice so ``cdk destroy``
        #   takes the table with it. Production would use ``RETAIN`` to
        #   preserve cached state across stack replacements.
        #
        # Data flow summary:
        #   - Written by: the Fargate response handler (task 5) on every
        #     successful read-tool response (write-through).
        #   - Read by: each domain agent (task 8) only on a per-tool
        #     timeout, as the fallback branch of the async shim.
        #   - Not read by: the gateway Lambdas (task 4) — they only write
        #     the initial ``pending`` record to ``CommandResults``.
        self.store_state_cache_table: dynamodb.Table = dynamodb.Table(
            self,
            "StoreStateCacheTable",
            table_name="StoreStateCache",
            partition_key=dynamodb.Attribute(
                name="store_domain_key",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=cdk.RemovalPolicy.DESTROY,  # production would use RETAIN
        )

        # --------------------------------------------------------------
        # Task 2.3 — Handoff attributes: table names + ARNs
        # --------------------------------------------------------------
        #
        # Downstream consumers almost always want one of two handles on
        # these tables:
        #
        # * the **name** — pushed into Lambda / ECS environment variables
        #   so the runtime code can issue ``DynamoDB:PutItem`` / ``UpdateItem``
        #   / ``GetItem`` against the right table, and emitted as a
        #   CloudFormation output (task 11) for smoke tests and consumers
        #   that resolve the table by name rather than by construct ref.
        # * the **ARN** — plugged into ``iam.PolicyStatement.resources``
        #   so least-privilege grants (tasks 4.4, 5.5, 10.2) can scope
        #   to exactly this table rather than ``dynamodb:*``.
        #
        # Exposing both alongside the ``_table`` attribute lets each
        # consumer grab the handle it needs without reaching through the
        # L2 construct. These are CDK tokens, not resolved strings — at
        # synth they render as ``{"Ref": "CommandResultsTable..."}``
        # (for the name) and ``{"Fn::GetAtt": [..., "Arn"]}`` (for the
        # ARN). Don't string-compare them at synth time; use
        # ``stack.resolve(...)`` in tests if you need to assert on shape.
        #
        # Consumed by:
        # * Task 4.3 / 4.4 — gateway Lambda env vars + execution-role
        #   policy statements.
        # * Task 5.4 / 5.5 — Fargate task definition env vars + task
        #   role policy statements.
        # * Task 11.1 — ``CommandResultsTableName`` / ``StoreStateCacheTableName``
        #   stack outputs.
        self.command_results_table_name: str = self.command_results_table.table_name
        self.command_results_table_arn: str = self.command_results_table.table_arn
        self.store_state_cache_table_name: str = self.store_state_cache_table.table_name
        self.store_state_cache_table_arn: str = self.store_state_cache_table.table_arn

        # --------------------------------------------------------------
        # Task 4.3 — Gateway Lambda functions (×4) (R4.1, R4.2, R4.7, R4.8)
        # --------------------------------------------------------------
        #
        # Four domain-specific MCP gateway Lambdas. Each Lambda exposes
        # the tools for one domain (pos, equipment, inventory, schedule)
        # via an MCP server handler. On each ``tools/call``, the Lambda
        # generates a correlation UUID, writes a pending record to
        # ``CommandResults``, publishes the command to the broker over
        # MQTT, and returns the correlation ID immediately — it never
        # blocks waiting for a response.
        #
        # All four share the same deployment package (``lambdas/``
        # directory, which contains both ``gateway/`` and ``shared/``).
        # The ``GATEWAY_DOMAIN`` environment variable tells the handler
        # which domain's tools to load at import time.
        #
        # Common configuration (R4.2, R4.7, R4.8):
        #   - Runtime: Python 3.11 on ARM64 (Graviton — cheaper).
        #   - Memory: 512 MB — headroom for cold-start + Secrets Manager
        #     + MQTT CONNECT + single PUBLISH.
        #   - Timeout: 10 s — generous for a publish-and-return flow.
        #   - Log retention: 14 days (R4.8, R11.1).
        #
        # Environment variables pushed to every function:
        #   - ``GATEWAY_DOMAIN`` — the domain name (pos / equipment /
        #     inventory / schedule). The handler uses this to load only
        #     that domain's tools from ``shared.tools``.
        #   - ``COMMAND_RESULTS_TABLE_NAME`` — physical name of the
        #     ``CommandResults`` DynamoDB table (from task 2.3).
        #
        # The transport is AWS IoT Core (connection-less ``iot-data:Publish``);
        # no broker endpoint/port/secret env vars are needed (spec 020 Task 8
        # removed the Amazon MQ path).
        #
        # The functions are stored in ``self.gateway_lambdas``, a dict
        # keyed by domain name, so task 4.4 (IAM roles), task 10.2
        # (agent roles with ``lambda:InvokeFunction``), and task 11.1
        # (stack outputs) can iterate over them.

        #: Domain names in alphabetical order. The function name for each
        #: domain is ``{domain}-tool-relay`` (R4.1, renamed 2026-08-08 from
        #: ``{domain}-mcp-gateway``: "MCP gateway" collided with AWS AgentCore
        #: Gateway, which IS an MCP tool-server, while these Lambdas do not speak
        #: MCP cloud-side at all -- they publish to IoT Core and correlate the
        #: response. R4.1 states the naming property; this is where it is pinned.
        _GATEWAY_DOMAINS: Final[list[str]] = [
            "equipment",
            "inventory",
            "pos",
            "schedule",
        ]

        self.gateway_lambdas: dict[str, aws_lambda.Function] = {}

        for domain in _GATEWAY_DOMAINS:
            function_name = f"{domain}-tool-relay"

            # ----------------------------------------------------------
            # Task 4.4 — Per-Lambda execution role (R9.1, R9.4, R9.5)
            # ----------------------------------------------------------
            #
            # Each gateway Lambda gets its own IAM role with exactly the
            # minimum permissions it needs. We do NOT use CDK's auto-
            # generated default role or any ``*FullAccess`` managed
            # policy. Every statement is resource-scoped — no ``*``
            # resources on any action.
            #
            # Permissions granted:
            #   1. ``dynamodb:PutItem``, ``dynamodb:UpdateItem``,
            #      ``dynamodb:GetItem`` on the ``CommandResults`` table
            #      only — explicitly NOT on ``StoreStateCache``.
            #   2. ``logs:CreateLogGroup``, ``logs:CreateLogStream``,
            #      ``logs:PutLogEvents`` scoped to the function's own
            #      CloudWatch log group.
            #   3. ``iot:Publish`` on this domain's command topic only
            #      (``stores/*/commands/{domain}``).
            #
            # The log group ARN uses the well-known Lambda log group
            # naming convention ``/aws/lambda/{function_name}``. CDK
            # creates the log group automatically when ``log_retention``
            # is set, so the ARN is predictable at synth time.
            role = iam.Role(
                self,
                f"GatewayLambdaRole-{domain}",
                assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
                description=(
                    f"Execution role for the {domain} MCP gateway Lambda. "
                    "Least-privilege: Secrets Manager read, CommandResults "
                    "DynamoDB read/write, own log group only."
                ),
            )

            # 1. DynamoDB — PutItem / UpdateItem / GetItem on
            #    CommandResults ONLY. The gateway Lambda never touches
            #    StoreStateCache (that's the response handler's job).
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="DynamoDBCommandResults",
                    actions=[
                        "dynamodb:PutItem",
                        "dynamodb:UpdateItem",
                        "dynamodb:GetItem",
                    ],
                    resources=[self.command_results_table_arn],
                )
            )

            # 2. CloudWatch Logs — create and write to the function's
            #    own log group only. The ARN pattern follows the Lambda
            #    default: /aws/lambda/{function_name}.
            log_group_arn = cdk.Arn.format(
                cdk.ArnComponents(
                    service="logs",
                    resource="log-group",
                    resource_name=f"/aws/lambda/{function_name}:*",
                    arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME,
                ),
                self,
            )
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="CloudWatchLogsOwnGroup",
                    actions=[
                        "logs:CreateLogGroup",
                        "logs:CreateLogStream",
                        "logs:PutLogEvents",
                    ],
                    resources=[log_group_arn],
                )
            )

            # 3. IoT Core publish (spec 020) — this gateway may publish only
            #    to its own domain's command topic, for any store.
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="IotPublishCommands",
                    actions=["iot:Publish"],
                    resources=[
                        f"arn:aws:iot:{self.region}:{self.account}:topic/stores/*/commands/{domain}"
                    ],
                )
            )

            fn = aws_lambda.Function(
                self,
                f"GatewayLambda-{domain}",
                function_name=function_name,
                # Spec 006 R4.2 requires a runtime AWS Lambda currently supports and
                # pins the version HERE rather than in prose, so moving forward is a
                # code change and not a spec amendment (R4.2 amended 2026-08-07 from a
                # permanent python3.11 pin, which expires at end of support). These
                # relays vendor nothing -- `lambdas/` holds only first-party modules and
                # boto3 comes from the runtime -- so a bump carries no ABI risk. Move it
                # forward before this version nears end of support; cdk-nag
                # AwsSolutions-L1 is the alarm and is deliberately NOT suppressed.
                runtime=aws_lambda.Runtime.PYTHON_3_14,
                architecture=aws_lambda.Architecture.ARM_64,
                handler="gateway.main.handler",
                code=aws_lambda.Code.from_asset("lambdas"),
                memory_size=512,
                timeout=cdk.Duration.seconds(10),
                log_retention=logs.RetentionDays.TWO_WEEKS,
                role=role,
                environment={
                    "GATEWAY_DOMAIN": domain,
                    "COMMAND_RESULTS_TABLE_NAME": self.command_results_table_name,
                },
            )
            self.gateway_lambdas[domain] = fn

            # --------------------------------------------------------------
            # cdk-nag suppressions for this domain's relay role (P1).
            #
            # Both AwsSolutions-IAM5 wildcards on this role are deliberate and
            # narrow:
            #
            #  1. logs `log-group:/aws/lambda/<this function>:*` -- the ':*' is
            #     the log-STREAM wildcard inside this function's OWN log group.
            #     Stream names are generated per execution, so they cannot be
            #     enumerated; the group itself is pinned to this function.
            #
            #  2. iot `topic/stores/*/commands/<domain>` -- the wildcard is over
            #     the STORE ID only, with the domain pinned. This is the
            #     fleet-scaling design, not a shortcut: one relay per domain may
            #     publish that domain's commands to any store, and cannot touch
            #     another domain's topics. It is the cloud->store half of the
            #     per-store isolation model whose store->cloud half is the
            #     ${iot:Connection.Thing.ThingName} fleet policy. Pinning store
            #     ids here would mean redeploying this stack per store onboarded.
            #
            # Suppressed by rule id WITHOUT `appliesTo`: the rendered resource
            # strings embed `<AWS::AccountId>`/region tokens whose form differs
            # between an env-agnostic synth and one with a concrete account, so an
            # appliesTo match is brittle. The blast radius is one role that has
            # exactly the two statements described above.
            # --------------------------------------------------------------
            NagSuppressions.add_resource_suppressions(
                role,
                [
                    {
                        "id": "AwsSolutions-IAM5",
                        "reason": (
                            f"Two intentional wildcards on the {domain} relay role. "
                            "(1) CloudWatch Logs is scoped to THIS function's own log "
                            "group; the ':*' suffix is the per-execution log-stream "
                            "wildcard, which cannot be enumerated at synth. "
                            "(2) iot:Publish is scoped to "
                            f"topic/stores/*/commands/{domain} -- the wildcard covers "
                            "the STORE ID only, with the domain pinned, which is the "
                            "per-domain isolation model that lets stores be onboarded "
                            "without redeploying this stack. Neither grant can reach "
                            "another domain's topics or another function's logs."
                        ),
                    }
                ],
                apply_to_children=True,
            )

        # --------------------------------------------------------------
        # cdk-nag: DDB3 is suppressed on both tables because spec 006 R3.4 makes
        # PITR-off a deliberate, justified requirement rather than an oversight.
        #
        # L1 is NOT suppressed. It used to be, on the grounds that R4.2 pinned
        # python3.11 and raising the runtime would silently repeal a documented
        # requirement. R4.2 was amended instead (2026-08-07): it now requires a
        # currently-supported runtime and pins the version in code, so L1 passes on
        # its own and stays available as the end-of-support alarm.
        # --------------------------------------------------------------
        for _table in (self.command_results_table, self.store_state_cache_table):
            NagSuppressions.add_resource_suppressions(
                _table,
                [
                    {
                        "id": "AwsSolutions-DDB3",
                        "reason": (
                            "Spec 006 R3.1/R3.2 require point-in-time recovery "
                            "DISABLED, and R3.4 records why: both tables hold "
                            "RECONSTRUCTIBLE data. CommandResults rows are ephemeral "
                            "correlation records that expire via the `expires_at` TTL, "
                            "and StoreStateCache is a write-through cache of "
                            "last-known store state -- both are rebuilt by normal "
                            "traffic, so a restore recovers nothing a live round-trip "
                            "does not. This is a data-character decision, NOT an "
                            "environment one: it does not flip in production. Contrast "
                            "web-push's subscription table, where PITR IS enabled, "
                            "because losing a subscription requires a human to "
                            "re-grant browser notification permission -- the one thing "
                            "traffic cannot rebuild. Changing this means amending "
                            "R3.1/R3.2/R3.4 first."
                        ),
                    }
                ],
            )

        # --------------------------------------------------------------
        # cdk-nag: the CDK-generated LogRetention helper (P1).
        #
        # `log_retention=` on the Lambdas makes CDK synthesize its own custom
        # resource + role to call logs:PutRetentionPolicy. We do not author that
        # role, and its permissions must span every log group it manages, so the
        # AWS managed policy (IAM4) and the `Resource::*` (IAM5) are properties
        # of the construct rather than choices made here.
        #
        # Located by walking the construct tree, NOT by
        # `add_resource_suppressions_by_path` with a literal path: that call
        # RAISES when the path matches nothing, and the path embeds the stack id
        # ("/CloudMcpGatewayStack/..."), which differs whenever the stack is
        # instantiated under another id -- as the unit tests do. Hardcoding it
        # turned every test that synthesizes this stack into an error.
        # --------------------------------------------------------------
        for _node in self.node.find_all():
            if _node.node.id.startswith("LogRetention"):
                NagSuppressions.add_resource_suppressions(
                    _node,
                    [
                        {
                            "id": "AwsSolutions-IAM4",
                            "reason": (
                                "CDK-generated LogRetention custom-resource role. "
                                "Created by aws-cdk-lib for `log_retention=` and not "
                                "authored here; it carries "
                                "AWSLambdaBasicExecutionRole for its own logging."
                            ),
                            "appliesTo": [
                                "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
                            ],
                        },
                        {
                            "id": "AwsSolutions-IAM5",
                            "reason": (
                                "CDK-generated LogRetention custom-resource role. It "
                                "calls logs:PutRetentionPolicy/DeleteRetentionPolicy "
                                "across the log groups it manages, which aws-cdk-lib "
                                "grants on '*'; the scope is set by the construct, not "
                                "by this stack."
                            ),
                            "appliesTo": ["Resource::*"],
                        },
                    ],
                    apply_to_children=True,
                )

        # --------------------------------------------------------------
        # Task 11.1 — Stack outputs (R13.1–R13.7)
        # --------------------------------------------------------------
        #
        # CloudFormation outputs with exact logical names as specified
        # in requirements R13. These outputs are consumed by:
        #   - Smoke tests and validation scripts (tasks 12, 13).
        #   - Future specs that build on top of this stack.
        #   - Developers invoking the supervisor agent via CLI/harness.

        # R13.3 — Gateway Lambda ARNs (one per domain).
        _GATEWAY_OUTPUT_NAMES: Final[dict[str, str]] = {
            "pos": "PosGatewayLambdaArn",
            "equipment": "EquipmentGatewayLambdaArn",
            "inventory": "InventoryGatewayLambdaArn",
            "schedule": "ScheduleGatewayLambdaArn",
        }

        for domain, output_name in _GATEWAY_OUTPUT_NAMES.items():
            cdk.CfnOutput(
                self,
                output_name,
                value=self.gateway_lambdas[domain].function_arn,
                description=(
                    f"ARN of the {domain} MCP gateway Lambda function"
                ),
            )

        # R13.4 — DynamoDB table names.
        cdk.CfnOutput(
            self,
            "CommandResultsTableName",
            value=self.command_results_table_name,
            description="Physical name of the CommandResults DynamoDB table",
        )

        cdk.CfnOutput(
            self,
            "StoreStateCacheTableName",
            value=self.store_state_cache_table_name,
            description="Physical name of the StoreStateCache DynamoDB table",
        )

        # R13.7 — Region (us-west-2).
        cdk.CfnOutput(
            self,
            "Region",
            value=REQUIRED_REGION,
            description="AWS region this stack is deployed in",
        )
