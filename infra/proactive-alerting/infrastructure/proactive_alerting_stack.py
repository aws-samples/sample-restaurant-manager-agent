# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ProactiveAlertingStack -- spec 023 Phase 0 (engine-independent foundation).

Stands up the cloud-side proactive alerting pipeline:
  - Alerts DynamoDB table (system of record) + DedupIndex GSI + TTL.
  - proactive-alerts EventBridge bus + rule on AlertRaised -> Step Functions.
  - Step Functions (Standard) policy orchestrator: severity / quiet-hours /
    critical-override decision (Task 5), a waitForTaskToken ack (Task 7), and a
    per-severity SLA timeout with bounded re-surface to the same display then
    expiry (Task 8). The re-surface counter lives in the execution state.
  - ingest Lambda (IoT rule stores/+/events/+ excluding events/ack): Task 1
    SHELL; validate / dedup / persist / emit is spec 023 Task 4.
  - ack Lambda (IoT rule stores/+/events/ack): Task 1 SHELL; task-token resume
    is spec 023 Task 7.
  - events SQS dead-letter queue (malformed events).

Least privilege (Recommendation Engine "Least Privilege Design", AWS-61):
explicit actions + resource ARNs scoped wherever the action supports it. The one
unavoidable Resource:"*" is the ack handler's states:SendTaskSuccess /
SendTaskFailure (these actions do NOT support resource-level scoping -- an AWS
limitation); every other statement is ARN-scoped.

Pin aws-cdk-lib==2.250.0.
"""
from __future__ import annotations

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_iot as iot
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from cdk_nag import NagSuppressions
from constructs import Construct

ALERT_EVENT_SOURCE = "proactive.alerts"
ALERT_RAISED_DETAIL_TYPE = "AlertRaised"
DEDUP_INDEX_NAME = "DedupIndex"


class ProactiveAlertingStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        ctx = self.node.try_get_context
        alerts_table_name = ctx("alertsTableName") or "Alerts"
        bus_name = ctx("eventBusName") or "proactive-alerts"
        ttl_days = int(ctx("alertsTtlDays") or 30)
        dedup_window_minutes = int(ctx("dedupWindowMinutes") or 15)
        quiet_start_hour = int(ctx("quietHoursStartHour") or 22)
        quiet_end_hour = int(ctx("quietHoursEndHour") or 6)
        max_renotify = int(ctx("maxRenotify") or 3)
        sla_critical_seconds = int(ctx("ackSlaCriticalMinutes") or 5) * 60
        sla_warning_seconds = int(ctx("ackSlaWarningMinutes") or 30) * 60

        # --- Alerts table (system of record) + dedup GSI -------------------
        alerts_table = ddb.Table(
            self,
            "AlertsTable",
            table_name=alerts_table_name,
            partition_key=ddb.Attribute(name="PK", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="SK", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            removal_policy=RemovalPolicy.DESTROY,  # dev stack; alerts are TTL'd transient data
        )
        alerts_table.add_global_secondary_index(
            index_name=DEDUP_INDEX_NAME,
            partition_key=ddb.Attribute(name="dedup_key", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="status", type=ddb.AttributeType.STRING),
            projection_type=ddb.ProjectionType.ALL,
        )

        # --- events dead-letter queue (malformed events) -------------------
        events_dlq = sqs.Queue(
            self,
            "EventsDlq",
            queue_name="proactive-alerts-events-dlq",
            retention_period=Duration.days(14),
            enforce_ssl=True,
        )

        # --- EventBridge bus ----------------------------------------------
        bus = events.EventBus(self, "AlertsBus", event_bus_name=bus_name)

        # --- Policy + Notify Lambdas (spec 023 Task 5) ---------------------
        policy_fn = lambda_.Function(
            self,
            "PolicyFn",
            function_name="proactive-alert-policy-eval",
            runtime=lambda_.Runtime.PYTHON_3_14,  # latest (cdk-nag L1); pure Python, nothing vendored
            architecture=lambda_.Architecture.ARM_64,
            handler="main.handler",
            code=lambda_.Code.from_asset("lambdas/policy"),
            timeout=Duration.seconds(15),
            memory_size=128,
            environment={
                "QUIET_HOURS_START_HOUR": str(quiet_start_hour),
                "QUIET_HOURS_END_HOUR": str(quiet_end_hour),
                "SLA_CRITICAL_SECONDS": str(sla_critical_seconds),
                "SLA_WARNING_SECONDS": str(sla_warning_seconds),
            },
        )  # decision only (severity gate + quiet hours + critical override + ack SLA); logs-only IAM.

        notify_fn = lambda_.Function(
            self,
            "NotifyFn",
            function_name="proactive-alert-notify",
            runtime=lambda_.Runtime.PYTHON_3_14,  # latest (cdk-nag L1); pure Python, nothing vendored
            architecture=lambda_.Architecture.ARM_64,
            handler="main.handler",
            code=lambda_.Code.from_asset("lambdas/notify"),
            timeout=Duration.seconds(15),
            memory_size=128,
            environment={"ALERTS_TABLE_NAME": alerts_table_name},
        )
        # Publish policy-approved notifications to the store (scoped; no Resource:*).
        notify_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="PublishStoreNotifications",
                actions=["iot:Publish"],
                resources=[f"arn:aws:iot:{self.region}:{self.account}:topic/stores/*/notifications/*"],
            )
        )
        # Persist the waitForTaskToken token on the alert row (spec 023 Task 7).
        notify_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="AlertsTokenPersist",
                actions=["dynamodb:UpdateItem"],
                resources=[alerts_table.table_arn],
            )
        )

        # --- Step Functions policy orchestrator (spec 023 Task 5/7/8) ------
        # EvaluatePolicy -> InitRenotify -> ShouldNotify:
        #   notify? -> Notify (publish + waitForTaskToken, timeout = per-severity SLA)
        #               - ack (SendTaskSuccess) -> Acknowledged
        #               - States.Timeout -> RenotifyDecision:
        #                     renotify_count < MAX -> IncrementRenotify -> Notify (loop; re-publish)
        #                     else                 -> MarkExpired (status=expired) -> Expired
        #   else     -> InboxOnly
        # The re-surface counter lives in the EXECUTION state; Notify re-publishes to
        # the SAME notifications topic on each loop. No routing to any other
        # person/surface, no second channel (#4).
        evaluate = tasks.LambdaInvoke(
            self, "EvaluatePolicy", lambda_function=policy_fn, output_path="$.Payload",
        )
        init_renotify = sfn.Pass(
            self, "InitRenotify",
            result=sfn.Result.from_number(0),
            result_path="$.renotify_count",
        )
        notify = tasks.LambdaInvoke(
            self,
            "Notify",
            lambda_function=notify_fn,
            integration_pattern=sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
            payload=sfn.TaskInput.from_object({
                "store_id": sfn.JsonPath.string_at("$.store_id"),
                "alert_id": sfn.JsonPath.string_at("$.alert_id"),
                "notification": sfn.JsonPath.object_at("$.notification"),
                "task_token": sfn.JsonPath.task_token,
            }),
            task_timeout=sfn.Timeout.at("$.sla_seconds"),  # per-severity ack SLA (Task 8)
            result_path="$.ack_result",
        )
        acknowledged = sfn.Succeed(
            self, "Acknowledged",
            comment="ack received within SLA; execution resumed via SendTaskSuccess",
        )
        inbox_only = sfn.Succeed(
            self, "InboxOnly",
            comment="info / quiet-hours: alert stays in the inbox (already persisted by ingest)",
        )
        # No ack within SLA after MAX re-surfaces -> terminal expired (stays in inbox).
        mark_expired = tasks.DynamoUpdateItem(
            self,
            "MarkExpired",
            table=alerts_table,
            key={
                "PK": tasks.DynamoAttributeValue.from_string(
                    sfn.JsonPath.format("STORE#{}", sfn.JsonPath.string_at("$.store_id"))
                ),
                "SK": tasks.DynamoAttributeValue.from_string(
                    sfn.JsonPath.format("ALERT#{}", sfn.JsonPath.string_at("$.alert_id"))
                ),
            },
            update_expression="SET #s = :e REMOVE task_token",
            expression_attribute_names={"#s": "status"},  # 'status' is a reserved word
            expression_attribute_values={":e": tasks.DynamoAttributeValue.from_string("expired")},
            result_path=sfn.JsonPath.DISCARD,
        )
        expired = sfn.Succeed(
            self, "Expired",
            comment="no ack within SLA after MAX re-surfaces (#4); alert remains in the inbox",
        )
        increment = sfn.Pass(
            self,
            "IncrementRenotify",
            parameters={
                "store_id": sfn.JsonPath.string_at("$.store_id"),
                "alert_id": sfn.JsonPath.string_at("$.alert_id"),
                "decision": sfn.JsonPath.object_at("$.decision"),
                "notification": sfn.JsonPath.object_at("$.notification"),
                "sla_seconds": sfn.JsonPath.number_at("$.sla_seconds"),
                "renotify_count": sfn.JsonPath.math_add(
                    sfn.JsonPath.number_at("$.renotify_count"), 1
                ),
            },
        )
        renotify_decision = sfn.Choice(self, "RenotifyDecision")
        renotify_decision.when(
            sfn.Condition.number_less_than("$.renotify_count", max_renotify), increment
        )
        renotify_decision.otherwise(mark_expired.next(expired))

        increment.next(notify)  # loop back: Notify re-publishes and re-arms the wait
        notify.add_catch(
            renotify_decision, errors=["States.Timeout"], result_path=sfn.JsonPath.DISCARD
        )
        notify.next(acknowledged)

        decide = sfn.Choice(self, "ShouldNotify")
        decide.when(sfn.Condition.boolean_equals("$.decision.notify", True), notify)
        decide.otherwise(inbox_only)

        # Execution logging + tracing (cdk-nag AwsSolutions-SF1 / SF2, P1).
        # Fixed rather than suppressed: this machine owns alert POLICY -- severity
        # gating, quiet hours, the waitForTaskToken human-ack pause and the bounded
        # re-surface loop. Without ALL-event logs, "why was this alert never
        # re-surfaced?" is unanswerable after the fact, because the decisions live
        # in state transitions rather than in any Lambda's logs. The ack path is
        # resumed out-of-band by a token lookup, so execution history is the only
        # place the pause/resume is visible.
        policy_log_group = logs.LogGroup(
            self,
            "AlertPolicyStateMachineLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        state_machine = sfn.StateMachine(
            self,
            "AlertPolicyStateMachine",
            state_machine_name="proactive-alert-policy",
            state_machine_type=sfn.StateMachineType.STANDARD,
            definition_body=sfn.DefinitionBody.from_chainable(
                evaluate.next(init_renotify).next(decide)
            ),
            timeout=Duration.hours(24),
            logs=sfn.LogOptions(
                destination=policy_log_group,
                level=sfn.LogLevel.ALL,
                include_execution_data=True,
            ),
            tracing_enabled=True,
        )

        # EventBridge rule: AlertRaised -> state machine. targets.SfnStateMachine
        # creates a states:StartExecution role scoped to this machine (no Resource:*).
        events.Rule(
            self,
            "AlertRaisedRule",
            event_bus=bus,
            rule_name="proactive-alert-raised",
            event_pattern=events.EventPattern(
                source=[ALERT_EVENT_SOURCE],
                detail_type=[ALERT_RAISED_DETAIL_TYPE],
            ),
            targets=[targets.SfnStateMachine(state_machine)],
        )

        # --- ingest Lambda (shell) + least-privilege role ------------------
        ingest_fn = lambda_.Function(
            self,
            "IngestFn",
            function_name="proactive-alert-ingest",
            runtime=lambda_.Runtime.PYTHON_3_14,  # latest (cdk-nag L1); pure Python, nothing vendored
            architecture=lambda_.Architecture.ARM_64,
            handler="main.handler",
            code=lambda_.Code.from_asset("lambdas/ingest"),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                "ALERTS_TABLE_NAME": alerts_table_name,
                "DEDUP_INDEX_NAME": DEDUP_INDEX_NAME,
                "EVENT_BUS_NAME": bus_name,
                "EVENTS_DLQ_URL": events_dlq.queue_url,
                "DEDUP_WINDOW_MINUTES": str(dedup_window_minutes),
                "ALERTS_TTL_DAYS": str(ttl_days),
            },
        )
        # DynamoDB: explicit actions + table & index ARNs. (table.grant covers
        # only the table ARN, so the dedup Query needs /index/* added explicitly.)
        ingest_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="AlertsTableWriteAndDedupQuery",
                actions=["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"],
                resources=[alerts_table.table_arn, f"{alerts_table.table_arn}/index/*"],
            )
        )
        bus.grant_put_events_to(ingest_fn)
        events_dlq.grant_send_messages(ingest_fn)

        # --- ack Lambda (shell) + least-privilege role ---------------------
        # NOTE: states:SendTaskSuccess/Failure only support Resource:"*" (no
        # resource-level scoping), so that grant is DEFERRED to spec 023 Task 7
        # to keep the skeleton free of Resource:"*". Task 1 grants only scoped ddb.
        ack_fn = lambda_.Function(
            self,
            "AckFn",
            function_name="proactive-alert-ack",
            runtime=lambda_.Runtime.PYTHON_3_14,  # latest (cdk-nag L1); pure Python, nothing vendored
            architecture=lambda_.Architecture.ARM_64,
            handler="main.handler",
            code=lambda_.Code.from_asset("lambdas/ack"),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                "ALERTS_TABLE_NAME": alerts_table_name,
                "DEDUP_INDEX_NAME": DEDUP_INDEX_NAME,
            },
        )
        ack_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="AlertsTableAckUpdate",
                actions=["dynamodb:GetItem", "dynamodb:Query", "dynamodb:UpdateItem"],
                resources=[alerts_table.table_arn, f"{alerts_table.table_arn}/index/*"],
            )
        )
        # states:SendTask* do NOT support resource-level scoping (AWS limitation),
        # so Resource:"*" is unavoidable here (spec 023 Task 7).
        ack_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="ResumeAlertExecution",
                actions=["states:SendTaskSuccess", "states:SendTaskFailure"],
                resources=["*"],
            )
        )

        # --- IoT role for the events DLQ error action ----------------------
        iot_dlq_role = iam.Role(
            self,
            "IotEventsErrorActionRole",
            assumed_by=iam.ServicePrincipal("iot.amazonaws.com"),
            description="IoT Topic Rule error action - send malformed events to the DLQ",
        )
        events_dlq.grant_send_messages(iot_dlq_role)

        # --- IoT Topic Rules: events (ingest) + ack ------------------------
        # The ingest rule excludes events/ack (topic(4) is the {kind} segment)
        # so one device events/* publish grant serves both; ack has its own rule.
        events_rule = iot.CfnTopicRule(
            self,
            "EventsIngestRule",
            rule_name="proactive_alert_events_rule",
            topic_rule_payload=iot.CfnTopicRule.TopicRulePayloadProperty(
                sql="SELECT *, topic() AS topic FROM 'stores/+/events/+' WHERE topic(4) <> 'ack'",
                aws_iot_sql_version="2016-03-23",
                rule_disabled=False,
                actions=[
                    iot.CfnTopicRule.ActionProperty(
                        lambda_=iot.CfnTopicRule.LambdaActionProperty(
                            function_arn=ingest_fn.function_arn
                        )
                    )
                ],
                error_action=iot.CfnTopicRule.ActionProperty(
                    sqs=iot.CfnTopicRule.SqsActionProperty(
                        queue_url=events_dlq.queue_url,
                        role_arn=iot_dlq_role.role_arn,
                        use_base64=False,
                    )
                ),
            ),
        )
        ingest_fn.add_permission(
            "AllowIotInvokeIngest",
            principal=iam.ServicePrincipal("iot.amazonaws.com"),
            action="lambda:InvokeFunction",
            source_arn=events_rule.attr_arn,
        )

        ack_rule = iot.CfnTopicRule(
            self,
            "AckRule",
            rule_name="proactive_alert_ack_rule",
            topic_rule_payload=iot.CfnTopicRule.TopicRulePayloadProperty(
                sql="SELECT *, topic() AS topic FROM 'stores/+/events/ack'",
                aws_iot_sql_version="2016-03-23",
                rule_disabled=False,
                actions=[
                    iot.CfnTopicRule.ActionProperty(
                        lambda_=iot.CfnTopicRule.LambdaActionProperty(
                            function_arn=ack_fn.function_arn
                        )
                    )
                ],
            ),
        )
        ack_fn.add_permission(
            "AllowIotInvokeAck",
            principal=iam.ServicePrincipal("iot.amazonaws.com"),
            action="lambda:InvokeFunction",
            source_arn=ack_rule.attr_arn,
        )

        # --- Outputs -------------------------------------------------------
        # ------------------------------------------------------------------
        # cdk-nag suppressions (P1). Fixed rather than suppressed: SF1 + SF2
        # (execution logging and tracing enabled) and L1 (runtimes bumped -- all
        # four handlers are pure Python with nothing vendored).
        # ------------------------------------------------------------------
        NagSuppressions.add_resource_suppressions(
            alerts_table,
            [
                {
                    "id": "AwsSolutions-DDB3",
                    "reason": (
                        "Point-in-time recovery is deliberately OFF. Two reasons. "
                        "(1) Retention: spec 023 R6.3 bounds retention of alert records "
                        "by the table's TTL (R3.3); PITR keeps up to 35 days of "
                        "continuous backups, which would extend the effective retention "
                        "of alert data past the bound the spec states, so enabling it is "
                        "a compliance decision rather than a free win. (2) The data is "
                        "self-healing: ingest raises alerts from a deterministic "
                        "time-bucketed dedup key, so a condition that is still true "
                        "re-fires and re-creates its row, and an orphaned "
                        "waitForTaskToken pause degrades by design -- the SLA timeout "
                        "fires and the machine re-notifies or expires. Losing the table "
                        "costs history, not correctness."
                    ),
                }
            ],
        )

        NagSuppressions.add_resource_suppressions(
            events_dlq,
            [
                {
                    "id": "AwsSolutions-SQS3",
                    "reason": (
                        "This queue IS the dead-letter queue -- it is where the ingest "
                        "and ack handlers route rejected events (malformed envelopes, "
                        "and store_id/topic mismatches, which are the cross-tenant "
                        "case). A DLQ for a DLQ would just move the terminal hop."
                    ),
                }
            ],
        )

        # --- IAM findings -------------------------------------------------
        # IAM4: CDK's default managed policy on each handler role.
        # IAM5: five distinct wildcards, each unavoidable for a different reason.
        for _fn in (ingest_fn, ack_fn, policy_fn, notify_fn):
            NagSuppressions.add_resource_suppressions(
                _fn,
                [
                    {
                        "id": "AwsSolutions-IAM4",
                        "reason": (
                            "AWSLambdaBasicExecutionRole is CDK's default Lambda "
                            "execution policy, scoped to the function's own log group. "
                            "Every other grant on these roles is an explicit "
                            "resource-scoped statement (this table, this topic, this "
                            "state machine)."
                        ),
                        "appliesTo": [
                            "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
                        ],
                    }
                ],
                apply_to_children=True,
            )

        NagSuppressions.add_resource_suppressions(
            ack_fn,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "states:SendTaskSuccess/SendTaskFailure CANNOT be "
                        "resource-scoped -- AWS does not support resource-level "
                        "permissions for them, so 'Resource: *' is the only valid "
                        "policy. This is the documented exception recorded in "
                        "architecture-decisions.md (Step Functions waitForTaskToken) "
                        "and tests/test_stack.py already exempts exactly these actions "
                        "from its no-Resource:* assertion. The GSI wildcard "
                        "(table/index/*) is CDK's grant for querying the "
                        "store+alert-id index, scoped to this table's indexes only."
                    ),
                }
            ],
            apply_to_children=True,
        )

        NagSuppressions.add_resource_suppressions(
            ingest_fn,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "table/index/* is CDK's grant for querying this table's "
                        "secondary indexes; index names are a property of the table, "
                        "not separately addressable resources, and the grant cannot "
                        "reach another table."
                    ),
                }
            ],
            apply_to_children=True,
        )

        NagSuppressions.add_resource_suppressions(
            notify_fn,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "iot:Publish on topic/stores/*/notifications/* -- the wildcards "
                        "cover the STORE ID and the per-notification id, which are "
                        "runtime values. This mirrors the commands-topic scoping on the "
                        "relay Lambdas and is the cloud->store half of the per-store "
                        "isolation model; pinning store ids would mean redeploying this "
                        "stack for every store onboarded."
                    ),
                }
            ],
            apply_to_children=True,
        )

        NagSuppressions.add_resource_suppressions(
            state_machine,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "Two wildcards, both added by CDK rather than authored here. "
                        "(1) '<Fn.Arn>:*' is grant_invoke's version/alias suffix for the "
                        "two Lambdas this machine calls -- each is pinned to one "
                        "function. (2) 'Resource: *' is the X-Ray grant that comes with "
                        "tracing_enabled=True; xray:PutTraceSegments and the sampling "
                        "APIs take no resource ARN. Note the trade-off: enabling "
                        "tracing to satisfy AwsSolutions-SF2 is what introduced this "
                        "wildcard, and observability was judged worth it for a state "
                        "machine whose decisions (quiet hours, ack pause, bounded "
                        "re-surface) are otherwise invisible after the fact."
                    ),
                }
            ],
            apply_to_children=True,
        )

        CfnOutput(self, "AlertsTableNameOut", value=alerts_table.table_name)
        CfnOutput(self, "AlertsBusNameOut", value=bus.event_bus_name)
        CfnOutput(self, "AlertPolicyStateMachineArn", value=state_machine.state_machine_arn)
        CfnOutput(self, "IngestFnArn", value=ingest_fn.function_arn)
        CfnOutput(self, "AckFnArn", value=ack_fn.function_arn)
        CfnOutput(self, "PolicyFnArn", value=policy_fn.function_arn)
        CfnOutput(self, "NotifyFnArn", value=notify_fn.function_arn)
        CfnOutput(self, "EventsDlqUrl", value=events_dlq.queue_url)
