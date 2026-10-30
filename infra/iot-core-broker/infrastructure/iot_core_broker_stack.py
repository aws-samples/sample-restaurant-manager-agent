# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""IotCoreBrokerStack — spec 020 Phase A (additive; nothing cuts over).

Stands up the connection-less cloud response path alongside Amazon MQ:
  - response-handler Lambda (productionized from the 019 spike; resolves the
    correlation, writes the terminal CommandResults row, and does the monotonic
    StoreStateCache write-through -- spec 006 R5.2/R5.3).
  - IoT Topic Rule on stores/+/responses/+ -> response-handler.

Wires to the EXISTING CommandResults / StoreStateCache tables via CDK context
(not re-created -- spec-006 outputs aren't CFN exports, steering note). The
per-store IoT policy + thing/cert provisioning live in Task 2.

The durable-agent callback resume this handler was originally built around
(spec 020 R3.1) was retired 2026-08-07 with the durable layer itself, along with
its cross-Lambda IAM grant and the vendored boto3 it needed (R3.4).
Pin aws-cdk-lib==2.250.0.
"""
from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_iot as iot
from aws_cdk import aws_lambda as lambda_
from cdk_nag import NagSuppressions
from constructs import Construct


class IotCoreBrokerStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        command_results = self.node.try_get_context("commandResultsTableName") or "CommandResults"
        state_cache = self.node.try_get_context("storeStateCacheTableName") or "StoreStateCache"

        cr_table = ddb.Table.from_table_name(self, "CommandResults", command_results)
        cache_table = ddb.Table.from_table_name(self, "StoreStateCache", state_cache)

        role = iam.Role(
            self,
            "ResponseHandlerRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="IoT response handler - resolve correlation, write terminal result, cache write-through",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )
        # Least-privilege DynamoDB access, written out rather than using
        # grant_read_write_data / grant_write_data: those convenience grants hand over
        # 12 and 5 actions respectively (Scan, DeleteItem, BatchWriteItem, stream
        # GetRecords...), and this handler makes exactly three calls -- GetItem and
        # UpdateItem on CommandResults, UpdateItem on StoreStateCache.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ResolveCommandResult",
                # GetItem to resolve the correlation; UpdateItem to record the
                # terminal outcome (spec 006 R5.2). The write was missing until
                # 2026-08-07, when the handler stopped depending on a durable
                # callback to resolve correlations.
                actions=["dynamodb:GetItem", "dynamodb:UpdateItem"],
                resources=[cr_table.table_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteStoreStateCache",
                # Monotonic conditional write-through for read tools (R5.3).
                actions=["dynamodb:UpdateItem"],
                resources=[cache_table.table_arn],
            )
        )

        # NOTE: this role once also held a "ResumeDurableAgentCallback" grant --
        # lambda:SendDurableExecutionCallbackSuccess/Failure on a `function:durable-*`
        # wildcard -- for the durable agent layer destroyed in spec 022 Task 6. The
        # grant went first (2026-08-05, surfaced by cdk-nag IAM5 on the wildcard),
        # then the code that called it (2026-08-07, with spec 020 R3.1/R3.4 retired).
        #
        # Kept as a comment because the ORDER caused a real outage in between: with
        # the grant and the consumer gone but the code still branching on
        # `callback_id`, the handler's early return skipped the two writes spec 006
        # R5.2/R5.3 require, so it silently resolved nothing for two days. Removing a
        # consumer means checking what was downstream of it, not just what it used.

        response_fn = lambda_.Function(
            self,
            "ResponseHandlerFn",
            function_name="iot-response-handler",
            # Latest available Python runtime, per cdk-nag AwsSolutions-L1 (P1).
            # The handler is pure Python + the runtime's boto3 (nothing is vendored
            # for this Lambda), so a runtime bump carries no native-wheel risk.
            # Keep in lockstep with LAMBDA_RUNTIME_BY_STACK in
            # scripts/lock_dependencies.py, which compiles this Lambda's lock for
            # the same version. L1 will re-flag when a newer runtime ships -- that
            # is the rule working, not noise: old runtimes reach end of support.
            runtime=lambda_.Runtime.PYTHON_3_14,
            architecture=lambda_.Architecture.ARM_64,
            handler="main.handler",
            code=lambda_.Code.from_asset("lambdas/response_handler"),
            timeout=Duration.seconds(30),
            memory_size=256,
            role=role,
            environment={
                "COMMAND_RESULTS_TABLE_NAME": command_results,
                "STORE_STATE_CACHE_TABLE_NAME": state_cache,
            },
        )

        rule = iot.CfnTopicRule(
            self,
            "ResponseRule",
            rule_name="iot_response_rule",
            topic_rule_payload=iot.CfnTopicRule.TopicRulePayloadProperty(
                sql="SELECT *, topic() AS topic FROM 'stores/+/responses/+'",
                aws_iot_sql_version="2016-03-23",
                rule_disabled=False,
                actions=[
                    iot.CfnTopicRule.ActionProperty(
                        lambda_=iot.CfnTopicRule.LambdaActionProperty(
                            function_arn=response_fn.function_arn
                        )
                    )
                ],
            ),
        )
        response_fn.add_permission(
            "AllowIotInvoke",
            principal=iam.ServicePrincipal("iot.amazonaws.com"),
            action="lambda:InvokeFunction",
            source_arn=rule.attr_arn,
        )

        CfnOutput(self, "ResponseHandlerFnArn", value=response_fn.function_arn)
        CfnOutput(self, "ResponseRuleArn", value=rule.attr_arn)

        # ------------------------------------------------------------------
        # cdk-nag suppressions (P1). Each one records WHY the risk is accepted;
        # an unjustified suppression is worse than the finding, because it
        # silences the check without anyone having judged it.
        #
        # Deliberately NOT suppressed here: AwsSolutions-IAM5 (the dead
        # `durable-*` wildcard was removed instead) and AwsSolutions-L1 (the
        # runtime was bumped instead). Fix beats suppress whenever the fix is
        # cheap.
        # ------------------------------------------------------------------
        NagSuppressions.add_resource_suppressions(
            role,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": (
                        "AWSLambdaBasicExecutionRole is the AWS-managed policy CDK "
                        "attaches by default for Lambda execution roles. Its scope is "
                        "CloudWatch Logs group/stream creation and PutLogEvents on the "
                        "function's own log group -- narrower than an inline policy a "
                        "reader would hand-write, and maintained by AWS. Every other "
                        "permission on this role is an explicit, resource-scoped inline "
                        "statement (DynamoDB read on CommandResults, write on "
                        "StoreStateCache)."
                    ),
                    "appliesTo": [
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
                    ],
                }
            ],
        )
