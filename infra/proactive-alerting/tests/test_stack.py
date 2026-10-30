# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CDK template assertions for ProactiveAlertingStack (spec 023 Task 1)."""
from __future__ import annotations

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from infrastructure.proactive_alerting_stack import ProactiveAlertingStack


def _template() -> Template:
    app = cdk.App()
    stack = ProactiveAlertingStack(app, "TestStack")
    return Template.from_stack(stack)


def test_alerts_table_key_schema_ttl_and_dedup_gsi():
    t = _template()
    t.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "KeySchema": [
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            "TimeToLiveSpecification": {"AttributeName": "ttl", "Enabled": True},
            "BillingMode": "PAY_PER_REQUEST",
            "GlobalSecondaryIndexes": Match.array_with(
                [
                    Match.object_like(
                        {
                            "IndexName": "DedupIndex",
                            "KeySchema": [
                                {"AttributeName": "dedup_key", "KeyType": "HASH"},
                                {"AttributeName": "status", "KeyType": "RANGE"},
                            ],
                            "Projection": {"ProjectionType": "ALL"},
                        }
                    )
                ]
            ),
        },
    )


def test_eventbridge_rule_matches_alert_raised():
    t = _template()
    t.has_resource_properties(
        "AWS::Events::Rule",
        {"EventPattern": {"source": ["proactive.alerts"], "detail-type": ["AlertRaised"]}},
    )


def test_state_machine_is_standard():
    t = _template()
    t.has_resource_properties("AWS::StepFunctions::StateMachine", {"StateMachineType": "STANDARD"})


def test_two_iot_rules_with_expected_sql():
    t = _template()
    rules = t.find_resources("AWS::IoT::TopicRule")
    sqls = [r["Properties"]["TopicRulePayload"]["Sql"] for r in rules.values()]
    assert len(sqls) == 2, f"expected 2 IoT rules, got {len(sqls)}"
    ingest = [s for s in sqls if "events/+" in s]
    ack = [s for s in sqls if s.endswith("events/ack'")]
    assert ingest and "topic(4) <> 'ack'" in ingest[0], "ingest rule must exclude events/ack"
    assert ack, "ack rule on stores/+/events/ack expected"


def test_events_dlq_present():
    _template().resource_count_is("AWS::SQS::Queue", 1)


#: Actions AWS does not support resource-level permissions for, so a policy
#: granting them MUST use ``Resource: "*"``. The test below exempts a statement
#: only when EVERY action in it is on this list -- so a wildcard resource can
#: never ride along with a data-access action.
#:
#: `states:SendTask*` was the original entry (resuming a waitForTaskToken pause;
#: see architecture-decisions.md).
#:
#: The `logs:*LogDelivery` / `logs:Describe*` / `logs:PutResourcePolicy` and
#: `xray:*` entries were added 2026-08-05 when cdk-nag AwsSolutions-SF1/SF2 were
#: fixed by enabling ALL-event execution logging and X-Ray tracing on the policy
#: state machine. Step Functions logging cannot be enabled without them, and none
#: of them grants access to application data -- they configure a log-delivery
#: channel and emit traces. That is a deliberate amendment to the "SendTask* is
#: the only Resource:*" note in architecture-decisions.md: the invariant is "no
#: wildcard over DATA", not "never call a resource-less API".
_UNSCOPABLE_ACTIONS = {
    "states:SendTaskSuccess",
    "states:SendTaskFailure",
    "states:SendTaskHeartbeat",
    # Step Functions -> CloudWatch Logs delivery (required by logs=LogOptions).
    "logs:CreateLogDelivery",
    "logs:GetLogDelivery",
    "logs:UpdateLogDelivery",
    "logs:DeleteLogDelivery",
    "logs:ListLogDeliveries",
    "logs:PutResourcePolicy",
    "logs:DescribeResourcePolicies",
    "logs:DescribeLogGroups",
    # X-Ray tracing (required by tracing_enabled=True).
    "xray:PutTraceSegments",
    "xray:PutTelemetryRecords",
    "xray:GetSamplingRules",
    "xray:GetSamplingTargets",
}


def test_no_iam_statement_uses_resource_star_or_action_wildcard():
    t = _template()
    for logical_id, policy in t.find_resources("AWS::IAM::Policy").items():
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            act = stmt["Action"]
            act_list = act if isinstance(act, list) else [act]
            # states:SendTask* cannot be resource-scoped (AWS limitation) -> Resource:* allowed.
            if set(act_list) <= _UNSCOPABLE_ACTIONS:
                continue
            res = stmt["Resource"]
            res_list = res if isinstance(res, list) else [res]
            assert "*" not in res_list, f"{logical_id}: literal Resource:'*' ({stmt.get('Sid')})"
            for a in act_list:
                assert not (isinstance(a, str) and (a == "*" or a.endswith(":*"))), (
                    f"{logical_id}: action wildcard {a}"
                )


def test_ack_role_can_resume_workflow():
    t = _template()
    found = False
    for policy in t.find_resources("AWS::IAM::Policy").values():
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
            if "states:SendTaskSuccess" in actions:
                found = True
    assert found, "ack handler must be able to resume the workflow via SendTaskSuccess"


def test_no_role_uses_fullaccess_or_admin_managed_policy():
    t = _template()
    for logical_id, role in t.find_resources("AWS::IAM::Role").items():
        for mp in role["Properties"].get("ManagedPolicyArns", []):
            s = str(mp)
            assert "FullAccess" not in s and "Administrator" not in s, (
                f"{logical_id}: over-broad managed policy {s}"
            )


def test_four_lambda_functions_present():
    t = _template()
    names = {
        r["Properties"]["FunctionName"]
        for r in t.find_resources("AWS::Lambda::Function").values()
        if "FunctionName" in r["Properties"]
    }
    assert {
        "proactive-alert-ingest", "proactive-alert-ack",
        "proactive-alert-policy-eval", "proactive-alert-notify",
    } <= names


def test_notify_role_has_scoped_iot_publish():
    t = _template()
    found = False
    for policy in t.find_resources("AWS::IAM::Policy").values():
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
            if "iot:Publish" in actions:
                res = stmt["Resource"]
                assert res != "*" and "notifications" in str(res)
                found = True
    assert found, "expected a scoped iot:Publish statement for notifications/*"


def _definition_str(t: Template) -> str:
    """Flatten the synthesized state-machine DefinitionString (a Fn::Join of literal
    parts + token refs) into a searchable string of its literal segments."""
    sm = list(t.find_resources("AWS::StepFunctions::StateMachine").values())[0]
    definition = sm["Properties"]["DefinitionString"]
    if isinstance(definition, str):
        return definition
    parts = definition["Fn::Join"][1]
    return "".join(p for p in parts if isinstance(p, str))


def test_state_machine_has_sla_timeout_and_bounded_renotify_loop():
    d = _definition_str(_template())
    # per-severity ack SLA on the waitForTaskToken wait
    assert "TimeoutSecondsPath" in d and "$.sla_seconds" in d
    # bounded re-surface loop (Task 8)
    assert "RenotifyDecision" in d
    assert "IncrementRenotify" in d
    assert "MarkExpired" in d
    assert "Expired" in d
    assert "States.Timeout" in d  # the no-ack catch
    assert "States.MathAdd($.renotify_count, 1)" in d  # in-state counter
    assert "NumericLessThan" in d  # renotify_count < MAX bound


def test_state_machine_role_can_mark_expired():
    """The MarkExpired DynamoUpdateItem grants the SM role dynamodb:UpdateItem,
    scoped to the Alerts table (the no-Resource:* test guards the scoping)."""
    t = _template()
    sm_role_ids = set()
    for lid, role in t.find_resources("AWS::IAM::Role").items():
        for s in role["Properties"]["AssumeRolePolicyDocument"]["Statement"]:
            if "states" in str(s.get("Principal", {}).get("Service", "")):
                sm_role_ids.add(lid)
    assert sm_role_ids, "expected a Step Functions execution role"
    found = False
    for policy in t.find_resources("AWS::IAM::Policy").values():
        roles = str(policy["Properties"].get("Roles", []))
        if not any(rid in roles for rid in sm_role_ids):
            continue
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
            if "dynamodb:UpdateItem" in actions:
                found = True
    assert found, "state machine role must have dynamodb:UpdateItem for MarkExpired"
