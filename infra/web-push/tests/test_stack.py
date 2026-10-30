# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CDK template assertions for WebPushStack (spec 025 Task 1d)."""
from __future__ import annotations

import re
from pathlib import Path

import aws_cdk as cdk
from aws_cdk.assertions import Template

from infrastructure.web_push_stack import WebPushStack


def _template() -> Template:
    return Template.from_stack(WebPushStack(cdk.App(), "TestStack"))


def test_subscriptions_table_schema_and_ttl():
    _template().has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "KeySchema": [
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            "TimeToLiveSpecification": {"AttributeName": "ttl", "Enabled": True},
            "BillingMode": "PAY_PER_REQUEST",
        },
    )


def test_vapid_secret_present():
    _template().resource_count_is("AWS::SecretsManager::Secret", 1)


def test_two_named_lambdas_present():
    t = _template()
    names = {
        r["Properties"]["FunctionName"]
        for r in t.find_resources("AWS::Lambda::Function").values()
        if "FunctionName" in r["Properties"]
    }
    assert {"web-push-registrar", "web-push-sender"} <= names


def test_lambdas_are_arm64():
    t = _template()
    for r in t.find_resources("AWS::Lambda::Function").values():
        if r["Properties"].get("FunctionName") in {"web-push-registrar", "web-push-sender", "web-push-vapid-rotator"}:
            assert r["Properties"]["Architectures"] == ["arm64"]


def test_two_iot_rules_with_expected_sql():
    t = _template()
    sqls = [
        r["Properties"]["TopicRulePayload"]["Sql"]
        for r in t.find_resources("AWS::IoT::TopicRule").values()
    ]
    assert len(sqls) == 2, f"expected 2 IoT rules, got {len(sqls)}"
    assert any(s.endswith("push-subscriptions'") for s in sqls), "subscribe rule missing"
    assert any("push-requests/+" in s for s in sqls), "push-request rule missing"
    # The handlers derive store_id ONLY from the authenticated topic (T1/T2),
    # so every rule must forward it.
    assert all("topic() AS topic" in s for s in sqls), "rules must forward topic()"


def test_no_iam_statement_uses_resource_star_or_action_wildcard():
    t = _template()
    for logical_id, policy in t.find_resources("AWS::IAM::Policy").items():
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            act = stmt["Action"]
            act_list = act if isinstance(act, list) else [act]
            res = stmt["Resource"]
            res_list = res if isinstance(res, list) else [res]
            assert "*" not in res_list, f"{logical_id}: literal Resource:'*' ({stmt.get('Sid')})"
            for a in act_list:
                assert not (isinstance(a, str) and (a == "*" or a.endswith(":*"))), (
                    f"{logical_id}: action wildcard {a}"
                )


def test_push_sender_has_scoped_vapid_secret_read():
    t = _template()
    found = False
    for policy in t.find_resources("AWS::IAM::Policy").values():
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            acts = stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
            if "secretsmanager:GetSecretValue" in acts:
                assert stmt["Resource"] != "*", "VAPID secret read must be scoped"
                found = True
    assert found, "expected a scoped secretsmanager:GetSecretValue statement"


def test_registrar_has_scoped_putitem():
    t = _template()
    found = False
    for policy in t.find_resources("AWS::IAM::Policy").values():
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            acts = stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
            if acts == ["dynamodb:PutItem"]:
                assert stmt["Resource"] != "*"
                found = True
    assert found, "expected a scoped dynamodb:PutItem statement for the registrar"


def test_push_sender_can_query_and_prune():
    t = _template()
    found = False
    for policy in t.find_resources("AWS::IAM::Policy").values():
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            acts = stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
            if "dynamodb:Query" in acts and "dynamodb:DeleteItem" in acts:
                assert stmt["Resource"] != "*"
                found = True
    assert found, "expected a scoped Query+DeleteItem statement for the push sender"


def test_no_role_uses_fullaccess_or_admin_managed_policy():
    t = _template()
    for logical_id, role in t.find_resources("AWS::IAM::Role").items():
        for mp in role["Properties"].get("ManagedPolicyArns", []):
            s = str(mp)
            assert "FullAccess" not in s and "Administrator" not in s, (
                f"{logical_id}: over-broad managed policy {s}"
            )


# --- spec 028: VAPID rotation ------------------------------------------------

def test_rotator_lambda_present_and_arm64():
    t = _template()
    rotator = [
        r for r in t.find_resources("AWS::Lambda::Function").values()
        if r["Properties"].get("FunctionName") == "web-push-vapid-rotator"
    ]
    assert rotator, "expected the web-push-vapid-rotator Lambda"
    assert rotator[0]["Properties"]["Architectures"] == ["arm64"]


def test_rotation_schedule_present():
    t = _template()
    rules = t.find_resources("AWS::Events::Rule")
    assert rules, "expected an EventBridge rule for scheduled VAPID rotation"
    assert any("ScheduleExpression" in r["Properties"] for r in rules.values())


def test_rotator_iam_scoped_to_secret_and_param():
    t = _template()
    put_secret = put_param = False
    for policy in t.find_resources("AWS::IAM::Policy").values():
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            acts = stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
            if "secretsmanager:PutSecretValue" in acts:
                assert stmt["Resource"] != "*", "rotator PutSecretValue must be scoped"
                put_secret = True
            if "ssm:PutParameter" in acts:
                assert stmt["Resource"] != "*", "rotator PutParameter must be scoped"
                put_param = True
    assert put_secret, "rotator must have scoped secretsmanager:PutSecretValue"
    assert put_param, "rotator must have scoped ssm:PutParameter"


# --- runtime / bundle-image / lock coherence ---------------------------------

_HERE = Path(__file__).resolve().parent
_BUNDLE_SH = _HERE.parent / "scripts" / "bundle.sh"
_LOCK_SCRIPT = _HERE.parents[2] / "scripts" / "lock_dependencies.py"


def test_bundle_image_matches_lambda_runtime():
    """One cpython version must be true in three places at once.

    ``scripts/bundle.sh`` vendors arm64 wheels in a ``build-python<X.Y>`` container,
    so the shipped ``.so`` files carry that cpython's ABI tag. A Lambda can only load
    extension modules built for its own runtime: if the bundle image and ``runtime=``
    disagree, every cold start fails with ``invalid ELF header`` -- and nothing at
    synth time notices, because ``Code.from_asset`` just uploads whatever is on disk.
    ``LAMBDA_RUNTIME_BY_STACK`` is the third place, since it decides which cpython
    the locks (and therefore the wheel tags in ``constraints.txt``) are compiled for.

    Bumping the runtime is a three-file change plus a re-bundle; this test is what
    makes forgetting one of them a test failure instead of a production outage.
    """
    bundle_version = re.search(
        r'^PY_VERSION="([0-9.]+)"', _BUNDLE_SH.read_text(), re.MULTILINE
    )
    assert bundle_version, "bundle.sh must declare PY_VERSION"
    expected = bundle_version.group(1)

    lock_entry = re.search(
        r'"web-push":\s*"([0-9.]+)"', _LOCK_SCRIPT.read_text()
    )
    assert lock_entry, "lock_dependencies.py must map web-push to a Lambda runtime"
    assert lock_entry.group(1) == expected, (
        f"locks target py{lock_entry.group(1)} but bundle.sh builds py{expected}"
    )

    cfn_runtime = f"python{expected}"
    for name, fn in _template().find_resources("AWS::Lambda::Function").items():
        assert fn["Properties"]["Runtime"] == cfn_runtime, (
            f"{name} runs {fn['Properties']['Runtime']} but the vendored wheels are "
            f"built for {cfn_runtime} by scripts/bundle.sh"
        )

