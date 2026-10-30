# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""WebPushStack -- spec 025 Task 1d (Web Push re-engagement).

Stands up the cloud half of Web Push delivery for the always-on display:

  - PushSubscriptions DynamoDB table (per-store browser push subscriptions).
  - A VAPID secret (Secrets Manager) holding the application-server private key
    + subject. The secret RESOURCE is created here; its VALUE is populated
    out-of-band by ``provision/gen_vapid.py`` (never in the template/source).
  - registrar Lambda (IoT rule ``stores/+/push-subscriptions``): upserts a
    browser subscription relayed by the store (the store has no inbound path,
    so the display POSTs to the store backend which republishes over IoT).
  - push-sender Lambda (IoT rule ``stores/+/push-requests/+``): on a store
    re-engagement request (emitted ONLY when the display is not actively
    connected -- the store owns that signal), sends a VAPID Web Push to every
    registered subscription for the store; prunes 404/410 (Gone) subscriptions.

Web Push is ADDITIVE to the spec-023 connected-path relay: the in-app inbox
remains the durable delivery floor; spoken delivery is the connected path; this
push only fires for a backgrounded/closed display.

Least privilege (Recommendation Engine "Least Privilege Design", AWS-61):
explicit actions + resource ARNs; no ``Resource:"*"`` and no action wildcards.
The browser holds no AWS credential; the VAPID private key never leaves the
cloud (Secrets Manager -> push-sender only).

Pin aws-cdk-lib==2.250.0.
"""
from __future__ import annotations

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_iot as iot
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_secretsmanager as secretsmanager
from cdk_nag import NagSuppressions
from constructs import Construct


class WebPushStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        ctx = self.node.try_get_context
        subs_table_name = ctx("pushSubscriptionsTableName") or "PushSubscriptions"
        vapid_secret_name = ctx("vapidSecretName") or "restaurant-sim/web-push/vapid"
        subs_ttl_days = int(ctx("pushSubscriptionTtlDays") or 90)
        # spec 028: current-public-key parameter (store-readable) + rotation cadence.
        vapid_public_key_param = ctx("vapidPublicKeyParamName") or "/restaurant-sim/web-push/vapid-public-key"
        vapid_rotation_days = int(ctx("vapidRotationDays") or 60)
        public_key_param_arn = (
            f"arn:aws:ssm:{self.region}:{self.account}:parameter{vapid_public_key_param}"
        )

        # --- Subscriptions table -------------------------------------------
        # PK = STORE#{store_id}, SK = SUB#{subscription_id}; subscription_id is a
        # stable hash of the push endpoint so a re-subscribe upserts in place.
        subs_table = ddb.Table(
            self,
            "PushSubscriptionsTable",
            table_name=subs_table_name,
            partition_key=ddb.Attribute(name="PK", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="SK", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            # Continuous backups / PITR (cdk-nag AwsSolutions-DDB3, P1). Enabled
            # rather than suppressed: a lost subscription table means every display
            # silently stops receiving push alerts until each one re-registers,
            # and re-registration needs a human to grant notification permission
            # again. PITR is cheap on a PAY_PER_REQUEST table this small and turns
            # an unrecoverable loss into a restore.
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.DESTROY,  # dev stack; subscriptions re-register
        )

        # --- VAPID secret (value provisioned out-of-band) ------------------
        # CDK creates the secret RESOURCE only. provision/gen_vapid.py generates
        # the EC P-256 keypair and writes {"private_key","public_key","subject"}
        # via PutSecretValue. The CDK-generated default value is never used.
        vapid_secret = secretsmanager.Secret(
            self,
            "VapidSecret",
            secret_name=vapid_secret_name,
            description="VAPID application-server keys for Web Push (private_key + subject); "
            "value populated out-of-band by provision/gen_vapid.py. Never bundled to the browser.",
        )

        # --- registrar Lambda ----------------------------------------------
        registrar_fn = lambda_.Function(
            self,
            "RegistrarFn",
            function_name="web-push-registrar",
            # Latest runtime (cdk-nag AwsSolutions-L1). All three Lambdas here run
            # the same version: the registrar vendors nothing (requirements.txt
            # declares no deps; boto3 comes from the runtime), and the two that DO
            # vendor arm64 wheels have them built for this cpython by
            # scripts/bundle.sh (PY_VERSION there must match).
            runtime=lambda_.Runtime.PYTHON_3_14,
            architecture=lambda_.Architecture.ARM_64,
            handler="main.handler",
            code=lambda_.Code.from_asset("lambdas/registrar"),
            timeout=Duration.seconds(15),
            memory_size=128,
            environment={
                "SUBSCRIPTIONS_TABLE_NAME": subs_table_name,
                "SUBSCRIPTION_TTL_DAYS": str(subs_ttl_days),
            },
        )
        registrar_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="UpsertSubscription",
                actions=["dynamodb:PutItem"],
                resources=[subs_table.table_arn],
            )
        )

        # --- push-sender Lambda --------------------------------------------
        # pywebpush (+ native cryptography) is bundled for arm64 at deploy time
        # (see lambda-steering / Task 1f); the unit tests mock it. The runtime must
        # match scripts/bundle.sh's PY_VERSION -- the vendored .so files are built
        # for that cpython ABI.
        push_sender_fn = lambda_.Function(
            self,
            "PushSenderFn",
            function_name="web-push-sender",
            runtime=lambda_.Runtime.PYTHON_3_14,
            architecture=lambda_.Architecture.ARM_64,
            handler="main.handler",
            code=lambda_.Code.from_asset("lambdas/push_sender"),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                "SUBSCRIPTIONS_TABLE_NAME": subs_table_name,
                "VAPID_SECRET_ARN": vapid_secret.secret_arn,
            },
        )
        # Read the store's subscriptions; prune Gone endpoints.
        push_sender_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="ReadAndPruneSubscriptions",
                actions=["dynamodb:Query", "dynamodb:DeleteItem"],
                resources=[subs_table.table_arn],
            )
        )
        # Read the VAPID private key (scoped to this secret ARN only).
        vapid_secret.grant_read(push_sender_fn)

        # --- rotator Lambda (spec 028): scheduled VAPID key rotation ---------
        # Regenerates the keypair into the secret and mirrors the PUBLIC key to
        # the SSM parameter the store backend reads. The parameter VALUE is
        # data-plane (seeded by provision/gen_vapid.py --put, updated here via
        # PutParameter Overwrite) -- deliberately NOT a CDK-managed value, so a
        # redeploy cannot clobber the live key (spec 028 design).
        rotator_fn = lambda_.Function(
            self,
            "RotatorFn",
            function_name="web-push-vapid-rotator",
            runtime=lambda_.Runtime.PYTHON_3_14,
            architecture=lambda_.Architecture.ARM_64,
            handler="main.handler",
            code=lambda_.Code.from_asset("lambdas/rotator"),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                "VAPID_SECRET_ARN": vapid_secret.secret_arn,
                "VAPID_PUBLIC_KEY_PARAM": vapid_public_key_param,
            },
        )
        # Rotate the keypair in place (read to preserve subject + write the new value).
        rotator_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="RotateVapidSecret",
                actions=["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"],
                resources=[vapid_secret.secret_arn],
            )
        )
        # Publish the new PUBLIC key to the store-readable parameter (scoped to it).
        rotator_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="PublishVapidPublicKey",
                actions=["ssm:PutParameter"],
                resources=[public_key_param_arn],
            )
        )
        # Schedule: rotate every `vapid_rotation_days` (default 60, safely < the
        # ARCC 90-day maximum). Also invokable on demand (aws lambda invoke).
        events.Rule(
            self,
            "VapidRotationSchedule",
            rule_name="web-push-vapid-rotation",
            schedule=events.Schedule.rate(Duration.days(vapid_rotation_days)),
            targets=[targets.LambdaFunction(rotator_fn)],
            description=f"Rotate the Web Push VAPID keypair every {vapid_rotation_days} days (spec 028).",
        )

        # --- IoT Topic Rules: subscribe (registrar) + request (sender) ------
        subscribe_rule = iot.CfnTopicRule(
            self,
            "PushSubscribeRule",
            rule_name="web_push_subscribe_rule",
            topic_rule_payload=iot.CfnTopicRule.TopicRulePayloadProperty(
                sql="SELECT *, topic() AS topic FROM 'stores/+/push-subscriptions'",
                aws_iot_sql_version="2016-03-23",
                rule_disabled=False,
                actions=[
                    iot.CfnTopicRule.ActionProperty(
                        lambda_=iot.CfnTopicRule.LambdaActionProperty(
                            function_arn=registrar_fn.function_arn
                        )
                    )
                ],
            ),
        )
        registrar_fn.add_permission(
            "AllowIotInvokeRegistrar",
            principal=iam.ServicePrincipal("iot.amazonaws.com"),
            action="lambda:InvokeFunction",
            source_arn=subscribe_rule.attr_arn,
        )

        request_rule = iot.CfnTopicRule(
            self,
            "PushRequestRule",
            rule_name="web_push_request_rule",
            topic_rule_payload=iot.CfnTopicRule.TopicRulePayloadProperty(
                sql="SELECT *, topic() AS topic FROM 'stores/+/push-requests/+'",
                aws_iot_sql_version="2016-03-23",
                rule_disabled=False,
                actions=[
                    iot.CfnTopicRule.ActionProperty(
                        lambda_=iot.CfnTopicRule.LambdaActionProperty(
                            function_arn=push_sender_fn.function_arn
                        )
                    )
                ],
            ),
        )
        push_sender_fn.add_permission(
            "AllowIotInvokePushSender",
            principal=iam.ServicePrincipal("iot.amazonaws.com"),
            action="lambda:InvokeFunction",
            source_arn=request_rule.attr_arn,
        )

        # --- Outputs -------------------------------------------------------
        # ------------------------------------------------------------------
        # cdk-nag suppressions (P1). Fixed rather than suppressed: DDB3 (PITR
        # enabled on the table) and L1 on the registrar (runtime bumped). What
        # remains is suppressed with the compensating control written down.
        # ------------------------------------------------------------------
        NagSuppressions.add_resource_suppressions(
            vapid_secret,
            [
                {
                    "id": "AwsSolutions-SMG4",
                    "reason": (
                        "VAPID rotation IS implemented -- just not via Secrets "
                        "Manager's native rotation, which is what this rule detects. "
                        "Spec 028 added a rotator Lambda on a 60-day EventBridge "
                        "schedule (plus on-demand invoke) that regenerates the EC "
                        "P-256 keypair, writes it back to this secret, and mirrors "
                        "ONLY the public key to an SSM parameter the display reads. "
                        "Native rotation does not fit: its staging-label lifecycle "
                        "(createSecret/setSecret/testSecret/finishSecret) has no step "
                        "for publishing a derived public value elsewhere, and a "
                        "half-rotated pair would leave the browser subscribed under a "
                        "key the sender no longer holds. The 60-day cadence is inside "
                        "the 90-day ARCC requirement this closed."
                    ),
                }
            ],
        )

        # NOTE: there is deliberately no AwsSolutions-L1 suppression here. Both
        # push_sender and rotator used to carry one because scripts/bundle.sh
        # vendored their arm64 wheels in a build-python3.12 container, which pinned
        # their runtime to 3.12. The bundle image now tracks the runtime, so the rule
        # passes on its own -- keeping the suppression would hide the next drift.

        # IAM4 on all three function service roles: CDK's default managed policy.
        for _fn in (registrar_fn, push_sender_fn, rotator_fn):
            NagSuppressions.add_resource_suppressions(
                _fn,
                [
                    {
                        "id": "AwsSolutions-IAM4",
                        "reason": (
                            "AWSLambdaBasicExecutionRole is the AWS-managed policy CDK "
                            "attaches by default for Lambda execution roles; its scope "
                            "is CloudWatch Logs creation and PutLogEvents on the "
                            "function's own log group. Every other permission on these "
                            "roles is an explicit resource-scoped inline statement "
                            "(this table, this secret, this SSM parameter)."
                        ),
                        "appliesTo": [
                            "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
                        ],
                    }
                ],
                apply_to_children=True,
            )

        CfnOutput(self, "PushSubscriptionsTableNameOut", value=subs_table.table_name)
        CfnOutput(self, "VapidSecretArnOut", value=vapid_secret.secret_arn)
        CfnOutput(self, "VapidSecretNameOut", value=vapid_secret_name)
        CfnOutput(self, "RegistrarFnArn", value=registrar_fn.function_arn)
        CfnOutput(self, "PushSenderFnArn", value=push_sender_fn.function_arn)
        CfnOutput(self, "VapidPublicKeyParamNameOut", value=vapid_public_key_param)
        CfnOutput(self, "RotatorFnArn", value=rotator_fn.function_arn)
