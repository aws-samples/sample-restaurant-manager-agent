# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Synth-time assertion tests for SimulatorEc2AlbStack (spec 004 + spec 029).

The tests run against a synthesized CloudFormation template so they are
fast, deterministic, and require no AWS credentials.

Spec 029 supersedes spec 007's ALB-terminated Cognito auth with a CloudFront +
Lambda@Edge (cognito-at-edge) design: an internal, HTTP-only ALB fronted by a
CloudFront distribution (default ``*.cloudfront.net`` cert) via a VPC origin, a
public Cognito app client, and no domain / Route 53 / ACM. These tests assert
that posture and guard against regressions.
"""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Final

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from infrastructure.simulator_ec2_alb_stack import (
    STACK_TAGS,
    SimulatorEc2AlbStack,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


#: Placeholder IoT ATS endpoint for test synths. ``iotEndpoint`` is REQUIRED at
#: synth time (it is account-specific and carries no default), so every synth
#: helper must supply one.
_EXAMPLE_IOT_ENDPOINT: Final[str] = "example-ats.iot.us-west-2.amazonaws.com"


def _harness_arn(account: str = "123456789012") -> str:
    """Harness ARN for a test synth.

    ``harnessArn`` is REQUIRED at synth time since spec 022 Task 6 made the
    AgentCore Managed Harness the only chat backend, so every synth helper must
    supply one whose account segment matches the synth account.
    """
    return f"arn:aws:bedrock-agentcore:us-west-2:{account}:harness/test_harness-AAAAAAAAAA"


def _synth() -> tuple[Template, dict]:
    """Synthesize the stack once and return both the ``Template`` assertion
    wrapper and the raw CloudFormation JSON. Used by the two module-scoped
    fixtures below so we pay the synth cost once per test module.

    Spec 029: passes the ``edgeCognito*`` context (phase-2 values) so the
    Lambda@Edge authenticator is created and associated with the CloudFront
    distribution's viewer-request event. A concrete ``env`` account is required
    for ``experimental.EdgeFunction`` to provision its us-east-1 support stack.
    The spec-007 ``domain_name`` context is gone (no domain / Route 53 / ACM).
    """
    app = cdk.App(context={
        "harnessArn": _harness_arn(),
        "iotEndpoint": _EXAMPLE_IOT_ENDPOINT,
        # This fixture covers the bootstrap branch (no AgentCore Memory), so it
        # opts out of the chat guard explicitly; TestChatFeatureContextGuard
        # covers the required path.
        "chatFeatureRequired": False,
        "edgeCognitoUserPoolId": "us-west-2_TESTPOOL0",
        "edgeCognitoAppClientId": "testappclientid0000000000",
        "edgeCognitoUserPoolDomain": "restaurant-sim-123456789012.auth.us-west-2.amazoncognito.com",
        "edgeCsrfNonceSecret": "test-only-nonce-secret",
        # Redirect the config.local.json sidecar write to a throwaway dir so this
        # test synth never clobbers the real, secret-bearing sidecar on disk.
        "edgeConfigDirOverride": tempfile.mkdtemp(prefix="edge-cfg-test-"),
    })
    stack = SimulatorEc2AlbStack(
        app,
        "TestStack",
        env=cdk.Environment(region="us-west-2", account="123456789012"),
    )
    raw = app.synth().get_stack_by_name("TestStack").template
    return Template.from_stack(stack), raw


@pytest.fixture(scope="module")
def template() -> Template:
    """``Template`` matcher wrapper for structural assertions."""
    return _synth()[0]


@pytest.fixture(scope="module")
def raw_template() -> dict:
    """Raw CloudFormation JSON for tests that need to walk every resource
    (e.g. the tag-coverage test) or inspect nested structures that the
    ``Template`` matcher API would make awkward."""
    return _synth()[1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_ec2_principal(doc: dict | None) -> bool:
    """Return ``True`` if the given trust policy is assumable by
    ``ec2.amazonaws.com``. Used to locate the one EC2 role in the stack."""
    if not doc:
        return False
    for stmt in doc.get("Statement", []):
        principal = stmt.get("Principal", {})
        if not isinstance(principal, dict):
            continue
        service = principal.get("Service")
        if service == "ec2.amazonaws.com":
            return True
        if isinstance(service, list) and "ec2.amazonaws.com" in service:
            return True
    return False


# ---------------------------------------------------------------------------
# 10.1 — no port 22 anywhere (R3.5, AC2.1)
# ---------------------------------------------------------------------------


def test_no_ssh_ingress(raw_template: dict) -> None:
    """R3.5 / AC2.1: no security group permits TCP 22 from any source.

    We stringify the whole template and look for the literal numeric tokens
    ``"FromPort": 22`` / ``"ToPort": 22``. That catches both inline
    ``SecurityGroupIngress`` rules on an ``AWS::EC2::SecurityGroup`` and
    standalone ``AWS::EC2::SecurityGroupIngress`` resources, which is where
    CDK puts cross-group rules.
    """
    blob = json.dumps(raw_template)
    assert '"FromPort": 22' not in blob, "port 22 ingress found in template"
    assert '"ToPort": 22' not in blob, "port 22 ingress found in template"


# ---------------------------------------------------------------------------
# 10.2 — listener + target group posture (R7.2, R7.6)
# ---------------------------------------------------------------------------


def test_single_http_listener_forwards(raw_template: dict) -> None:
    """Spec 029, Task 3: the ALB has exactly one listener - HTTP:80 - and it
    forwards to the target group (no redirect, no HTTPS). TLS + auth moved to
    CloudFront / the edge."""
    listeners = [
        v for v in raw_template["Resources"].values()
        if v["Type"] == "AWS::ElasticLoadBalancingV2::Listener"
    ]
    assert len(listeners) == 1, (
        f"expected exactly one listener, found {len(listeners)}"
    )
    props = listeners[0]["Properties"]
    assert props.get("Port") == 80, f"expected port 80, got {props.get('Port')}"
    assert props.get("Protocol") == "HTTP", (
        f"expected HTTP protocol, got {props.get('Protocol')}"
    )
    actions = props["DefaultActions"]
    assert len(actions) == 1, f"expected one action, got {len(actions)}"
    assert actions[0]["Type"] == "forward", (
        f"expected forward action, got {actions[0]['Type']}"
    )


def test_target_group_health_check(template: Template) -> None:
    """R7.6: target group is HTTP:8000 with the /api/health health check
    and the 30s/5s/2-of-2 posture from the requirements."""
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::TargetGroup",
        {
            "Protocol": "HTTP",
            "Port": 8000,
            "TargetType": "instance",
            "HealthCheckPath": "/api/health",
            "HealthCheckProtocol": "HTTP",
            "HealthCheckIntervalSeconds": 30,
            "HealthCheckTimeoutSeconds": 5,
            "HealthyThresholdCount": 2,
            "UnhealthyThresholdCount": 2,
        },
    )


# ---------------------------------------------------------------------------
# 10.3 — stickiness (R7.7)
# ---------------------------------------------------------------------------


def test_target_group_stickiness(template: Template) -> None:
    """R7.7: target group has ``lb_cookie`` stickiness enabled."""
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::TargetGroup",
        {
            "TargetGroupAttributes": Match.array_with(
                [
                    {"Key": "stickiness.enabled", "Value": "true"},
                    {"Key": "stickiness.type", "Value": "lb_cookie"},
                ]
            )
        },
    )


# ---------------------------------------------------------------------------
# 10.4 — IAM role scope (R6.2, R6.3, AC2.5)
# ---------------------------------------------------------------------------


def _ec2_role(raw_template: dict) -> dict:
    """Locate the single EC2-assumable IAM role in the template."""
    roles = [
        v
        for v in raw_template["Resources"].values()
        if v["Type"] == "AWS::IAM::Role"
        and _is_ec2_principal(v["Properties"].get("AssumeRolePolicyDocument"))
    ]
    assert len(roles) == 1, f"expected exactly one EC2 role, found {len(roles)}"
    return roles[0]


def test_ec2_role_has_ssm_managed_policy(raw_template: dict) -> None:
    """R6.2: the EC2 role has ``AmazonSSMManagedInstanceCore`` attached."""
    role = _ec2_role(raw_template)
    managed = json.dumps(role["Properties"].get("ManagedPolicyArns", []))
    assert "AmazonSSMManagedInstanceCore" in managed, (
        "AmazonSSMManagedInstanceCore managed policy not attached to EC2 role"
    )


def test_secrets_policy_is_scoped_to_one_arn(raw_template: dict) -> None:
    """R6.3 / AC2.5 (amended 2026-09-13): the stack grants ``GetSecretValue`` on
    exactly ONE secret -- the IoT device certificate bundle of R8.2.

    Previously this asserted a ``ReadMqttSecret`` inline policy on the role,
    scoped to the Amazon MQ placeholder secret. That secret was retired with the
    broker (spec 020 / ADR-004) and removed here, so the assertion now targets the
    surviving grant.

    Two structural differences from the old test, both deliberate:

    * the grant is an ``attach_inline_policy`` (a standalone ``AWS::IAM::Policy``),
      not an entry in the role's ``Policies``, so this scans EVERY policy statement
      in the template. That also makes the count assertion meaningful: it proves
      there is no *second* Secrets Manager grant hiding anywhere.
    * the resource legitimately ends in ``-*``. That is Secrets Manager's generated
      6-character ARN suffix, not a name wildcard, so a blanket "no ``*``" check
      would be wrong. The assertion below pins it to exactly that trailing form.
    """
    secret_stmts = [
        s
        for s in _all_policy_statements(raw_template)
        if any(a.startswith("secretsmanager:") for a in _actions(s))
    ]
    assert len(secret_stmts) == 1, (
        f"expected exactly one Secrets Manager grant in the stack, got "
        f"{len(secret_stmts)}: {json.dumps(secret_stmts)[:600]}"
    )

    stmt = secret_stmts[0]
    assert sorted(_actions(stmt)) == ["secretsmanager:GetSecretValue"], (
        f"unexpected Secrets Manager actions: {_actions(stmt)!r}"
    )

    resources = _resources(stmt)
    assert len(resources) == 1, f"expected one resource, got {resources!r}"
    resource = resources[0]
    assert resource != "*", "secrets policy must not use a wildcard resource"

    serialized = json.dumps(resource)
    assert "restaurant-sim/iot/" in serialized, (
        f"the surviving grant should target the IoT cert secret, got {serialized}"
    )
    # The ONLY wildcard permitted is the trailing ARN-suffix match.
    assert serialized.count("*") == 1, (
        f"secrets policy resource has more than the trailing ARN suffix wildcard: {serialized}"
    )
    assert "/cert-*" in serialized, (
        f"expected the wildcard to be the trailing '-*' ARN suffix, got {serialized}"
    )


def test_no_retired_mqtt_credentials_secret(raw_template: dict) -> None:
    """R8.1 / R6.3 / R10.6 regression guard: the retired Amazon MQ placeholder
    secret, its IAM grant, and its output are all gone -- and stay gone.

    This is the finding from the 2026-09-13 scan expressed as a test. The secret
    held ``{}`` in every deployment and nothing read it, yet it still carried a
    live ``GetSecretValue`` grant on the EC2 role. Deleting the resource without
    pinning its absence would let a future "restore the placeholder" change walk
    it straight back in, along with the permission.
    """
    # R8.1: this stack creates no secret at all. The store's IoT certificate is
    # owned by infra/iot-core-broker precisely so `cdk destroy` here cannot
    # revoke a store's identity.
    secrets = [
        lid
        for lid, res in raw_template["Resources"].items()
        if res.get("Type") == "AWS::SecretsManager::Secret"
    ]
    assert not secrets, f"stack must not create a Secrets Manager secret, found: {secrets}"

    # R6.3: no policy named for the retired grant, anywhere.
    for lid, res in raw_template["Resources"].items():
        if res.get("Type") == "AWS::IAM::Policy":
            assert "ReadMqttSecret" not in lid, f"retired ReadMqttSecret policy present: {lid}"
        if res.get("Type") == "AWS::IAM::Role":
            names = [
                p.get("PolicyName") for p in res.get("Properties", {}).get("Policies", [])
            ]
            assert "ReadMqttSecret" not in names, (
                f"retired ReadMqttSecret inline policy present on {lid}: {names}"
            )

    # Nothing should reference the retired secret's name any more.
    assert "restaurant-sim/mqtt/credentials" not in json.dumps(raw_template), (
        "the retired Amazon MQ credentials secret name still appears in the template"
    )

    # R12.4: user-data must carry exactly ONE transport-credential branch.
    #
    # Asserted on the MECHANISM, not on the token: a bare "MQTT_SECRET_ARN" not in
    # user_data check also matches the comment that DOCUMENTS the removed branch,
    # which would force the explanation to be contorted to satisfy the test. What
    # actually matters is that no CDK placeholder is rendered, no shell variable is
    # assigned, and there is exactly one secret fetch (the IoT cert bundle).
    user_data = json.dumps(
        [
            res.get("Properties", {}).get("UserData")
            for res in raw_template["Resources"].values()
            if res.get("Type") == "AWS::EC2::Instance"
        ]
    )
    assert "__MQTT_SECRET_ARN__" not in user_data, (
        "CDK still renders the retired __MQTT_SECRET_ARN__ placeholder into user-data"
    )
    assert "MQTT_SECRET_ARN=" not in user_data, (
        "user-data still assigns MQTT_SECRET_ARN (retired Amazon MQ path)"
    )
    fetches = user_data.count("aws secretsmanager get-secret-value")
    assert fetches == 1, (
        f"user-data should fetch exactly one secret (the IoT cert bundle), "
        f"found {fetches} `aws secretsmanager get-secret-value` calls"
    )


def test_tool_metrics_policy_scoped_to_namespace(raw_template: dict) -> None:
    """AGENTPERF06: the EC2 role may publish per-tool metrics, but
    ``cloudwatch:PutMetricData`` (which cannot be resource-scoped) is
    conditioned to the project metric-namespace prefix — no unconditioned
    metric publishing."""
    putm = []
    for r in raw_template["Resources"].values():
        if r["Type"] != "AWS::IAM::Policy":
            continue
        for s in r["Properties"]["PolicyDocument"]["Statement"]:
            actions = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
            if "cloudwatch:PutMetricData" in actions:
                putm.append(s)
    assert len(putm) == 1, f"expected one PutMetricData statement, got {len(putm)}"
    stmt = putm[0]
    cond = stmt.get("Condition", {})
    assert (
        cond.get("StringLike", {}).get("cloudwatch:namespace") == "RestaurantSim/*"
    ), f"PutMetricData must be namespace-conditioned, got {cond!r}"
    assert stmt.get("Resource") == "*", (
        "PutMetricData uses Resource:* (cannot be resource-scoped); the "
        "namespace Condition is what bounds it"
    )


def _synth_with_cache_table(table_name: str) -> dict:
    """Synthesize the stack with the spec-026 ``storeStateCacheTableName``
    context set; return the raw CloudFormation JSON."""
    app = cdk.App(context={
        "harnessArn": _harness_arn(),
        "iotEndpoint": _EXAMPLE_IOT_ENDPOINT,
        "chatFeatureRequired": False,
        "edgeAuthRequired": False,
        "storeStateCacheTableName": table_name,
    })
    stack = SimulatorEc2AlbStack(
        app,
        "TestStackCache",
        env=cdk.Environment(region="us-west-2", account="123456789012"),
    )
    return app.synth().get_stack_by_name("TestStackCache").template


def _ddb_statements(raw: dict) -> list:
    out = []
    for r in raw["Resources"].values():
        if r["Type"] != "AWS::IAM::Policy":
            continue
        for s in r["Properties"]["PolicyDocument"]["Statement"]:
            actions = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
            if any(a.startswith("dynamodb:") for a in actions):
                out.append(s)
    return out


def test_store_state_cache_grant_present_and_scoped_when_context_set() -> None:
    """spec 026 R6: with ``storeStateCacheTableName`` set, the EC2 role gains
    ``GetItem``/``PutItem`` scoped to exactly the StoreStateCache table ARN."""
    stmts = _ddb_statements(_synth_with_cache_table("StoreStateCache"))
    assert len(stmts) == 1, f"expected one DynamoDB statement, got {len(stmts)}"
    stmt = stmts[0]
    actions = stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
    assert set(actions) == {"dynamodb:GetItem", "dynamodb:PutItem"}
    res = json.dumps(stmt["Resource"])
    assert "table/StoreStateCache" in res
    assert "*" not in res, f"DynamoDB grant must be scoped (no wildcard): {res}"


def test_store_state_cache_grant_absent_when_context_empty(raw_template: dict) -> None:
    """spec 026 R5.2/R6: with no ``storeStateCacheTableName`` (default), the
    feature is off and no DynamoDB grant is attached (gated-policy pattern)."""
    assert _ddb_statements(raw_template) == []


def test_agent_tools_dashboard_present(raw_template: dict) -> None:
    """AGENTPERF06 #4: a CloudWatch dashboard for the per-tool metrics is
    created by default (``toolMetricsNamespace`` defaults to a non-empty value)."""
    dashboards = [
        r for r in raw_template["Resources"].values()
        if r["Type"] == "AWS::CloudWatch::Dashboard"
    ]
    assert len(dashboards) == 1
    assert dashboards[0]["Properties"].get("DashboardName") == "RestaurantSim-AgentTools"


def test_aggregate_timeout_alarm_present_not_search(raw_template: dict) -> None:
    """AGENTPERF06 #4: the aggregate tool-timeout alarm exists on the dimensionless
    ToolTimeoutsTotal metric -- a plain Metric Alarm, NOT a SEARCH() expression.
    CloudWatch Metric Alarms reject SEARCH (dashboard-only); the earlier
    SUM(SEARCH(...)) version failed at CREATE and was replaced (2026-07-06,
    todo 2026-07-06-agentperf06-aggregate-timeout-alarm)."""
    alarms = [
        r for r in raw_template["Resources"].values()
        if r["Type"] == "AWS::CloudWatch::Alarm"
    ]
    high = [
        a["Properties"] for a in alarms
        if a["Properties"].get("AlarmName") == "RestaurantSim-AgentTools-HighTimeouts"
    ]
    assert len(high) == 1, "expected the aggregate tool-timeout alarm"
    props = high[0]
    assert props.get("MetricName") == "ToolTimeoutsTotal"   # dimensionless aggregate
    assert "Metrics" not in props                            # plain metric, not a math/SEARCH expr
    # Regression guard: no Metric Alarm anywhere embeds a SEARCH() expression.
    for a in alarms:
        for m in a["Properties"].get("Metrics", []) or []:
            expr = m.get("Expression")
            assert not (isinstance(expr, str) and "SEARCH(" in expr), (
                f"{a['Properties'].get('AlarmName')} uses SEARCH(), unsupported on Metric Alarms"
            )


# ---------------------------------------------------------------------------
# 10.5 — every AWS::* resource carries the four tags (R1.4)
# ---------------------------------------------------------------------------

#: CloudFormation resource types that have no ``Tags`` property in their
#: schema. Asserting tags on these would be a false negative.
_UNTAGGABLE_TYPES: Final[frozenset[str]] = frozenset(
    {
        "AWS::IAM::Policy",
        "AWS::CloudWatch::Dashboard",
        "AWS::IAM::InstanceProfile",
        "AWS::EC2::SecurityGroupIngress",
        "AWS::EC2::SecurityGroupEgress",
        "AWS::EC2::SubnetRouteTableAssociation",
        "AWS::EC2::Route",
        "AWS::EC2::VPCGatewayAttachment",
        "AWS::EC2::EIPAssociation",
        "AWS::ElasticLoadBalancingV2::Listener",
        "AWS::ElasticLoadBalancingV2::ListenerRule",
        "AWS::SecretsManager::ResourcePolicy",
        "AWS::Lambda::Permission",
        # Spec 007: Cognito resources that don't support tags via CDK
        "AWS::Cognito::UserPoolClient",
        "AWS::Cognito::UserPoolDomain",
        # Route 53 record sets don't support tags
        "AWS::Route53::RecordSet",
        # ACM certificate validation DNS records (custom resource)
        "AWS::CloudFormation::CustomResource",
    }
)


def test_all_resources_tagged(raw_template: dict) -> None:
    """R1.4: every taggable resource inherits the four stack-level tags."""
    required = STACK_TAGS
    missing: list[str] = []
    for logical_id, resource in raw_template["Resources"].items():
        rtype = resource["Type"]
        if rtype in _UNTAGGABLE_TYPES or rtype.startswith("Custom::"):
            continue
        # CDK custom-resource provider singletons (the cross-region ARN reader
        # that experimental.EdgeFunction adds, and AwsCustomResource providers)
        # are framework-generated plumbing that the stack-level Tags aspect does
        # not reach - they are not resources this stack authors.
        if "CustomResourceProvider" in logical_id:
            continue
        props = resource.get("Properties", {}) or {}
        # Cognito User Pool uses ``UserPoolTags`` (a dict) instead of the
        # standard ``Tags`` (a list of {Key, Value} pairs).
        if rtype == "AWS::Cognito::UserPool":
            pool_tags = props.get("UserPoolTags", {})
            for key, value in required.items():
                if pool_tags.get(key) != value:
                    missing.append(f"{logical_id} ({rtype}): missing {key}={value}")
            continue
        tags = props.get("Tags")
        if not isinstance(tags, list):
            missing.append(f"{logical_id} ({rtype}): no Tags field")
            continue
        keys = {t["Key"]: t["Value"] for t in tags if isinstance(t, dict)}
        for key, value in required.items():
            if keys.get(key) != value:
                missing.append(f"{logical_id} ({rtype}): missing {key}={value}")
    assert not missing, "untagged resources:\n  " + "\n  ".join(missing)


# ---------------------------------------------------------------------------
# 10.6 — stack outputs (R10.1–R10.6 + spec 007 R9.1–R9.5)
# ---------------------------------------------------------------------------

_EXPECTED_OUTPUTS: Final[tuple[str, ...]] = (
    "InstanceId",
    "Region",
    "VpcId",
    "Ec2SecurityGroupId",
    "CognitoUserPoolId",
    "CognitoAppClientId",
    # Spec 029 additions (CloudFront is the public entry point)
    "CloudFrontDomainName",
    "CloudFrontUrl",
)

#: Outputs deliberately removed by later specs.
#:   * Spec 029 (Task 11.8): the domain-based Cognito URLs and the ALB DNS name
#:     (the internal ALB is no longer the entry point).
#:   * 2026-09-13 (R10.6): ``MqttSecretArn``, with the retired Amazon MQ
#:     placeholder secret it exported. Its only consumer was the spec 005 broker,
#:     decommissioned in spec 020.
_REMOVED_OUTPUTS: Final[tuple[str, ...]] = (
    "AlbDnsName",
    "CognitoDomainUrl",
    "CognitoLoginUrl",
    "MqttSecretArn",
)


def test_stack_outputs_present(raw_template: dict) -> None:
    """Spec 029 R7 / Task 11.8: the expected outputs are present (incl.
    CloudFrontDomainName + CloudFrontUrl), the removed domain-based outputs are
    gone, no custom-domain resources remain, and ``Region`` is ``us-west-2``."""
    outputs = raw_template.get("Outputs", {})
    for name in _EXPECTED_OUTPUTS:
        assert name in outputs, f"missing output: {name} (have: {sorted(outputs)})"
    for name in _REMOVED_OUTPUTS:
        assert name not in outputs, f"output {name} should have been removed"
    assert outputs["Region"]["Value"] == "us-west-2", (
        f"Region output must be 'us-west-2', got {outputs['Region']['Value']!r}"
    )
    # Spec 029 dropped the custom domain entirely (free *.cloudfront.net cert,
    # no Route 53, no ACM), superseding spec 007. Assert the general property --
    # no domain/certificate resources of any kind -- rather than blocklisting
    # the one hostname that used to be configured.
    domain_types = {
        "AWS::Route53::HostedZone",
        "AWS::Route53::RecordSet",
        "AWS::Route53::RecordSetGroup",
        "AWS::CertificateManager::Certificate",
    }
    offenders = {
        lid: res["Type"]
        for lid, res in raw_template["Resources"].items()
        if res["Type"] in domain_types
    }
    assert not offenders, f"custom-domain resources should be gone: {offenders}"


# ---------------------------------------------------------------------------
# Spec 007 — Cognito User Pool (R1.1–R1.6)
# ---------------------------------------------------------------------------


def test_cognito_user_pool_exists(template: Template) -> None:
    """Spec 029 (R4, reused from spec 007): Cognito User Pool with self-signup
    and email auto-verification. Self-signup => AdminCreateUserConfig with
    AllowAdminCreateUserOnly false."""
    template.has_resource_properties(
        "AWS::Cognito::UserPool",
        {
            "AutoVerifiedAttributes": ["email"],
            "UsernameAttributes": ["email"],
            "AdminCreateUserConfig": {"AllowAdminCreateUserOnly": False},
        },
    )


# ---------------------------------------------------------------------------
# Spec 029, Task 11.7 — public app client (D2-A, no secret)
# ---------------------------------------------------------------------------


def test_app_client_is_public_no_secret(raw_template: dict) -> None:
    """Spec 029 R4.1 / D2-A: the Cognito app client is PUBLIC - it does not
    generate a client secret (GenerateSecret is false/absent) and its callback
    URL points at the CloudFront domain, not a custom domain."""
    clients = [
        v for v in raw_template["Resources"].values()
        if v["Type"] == "AWS::Cognito::UserPoolClient"
    ]
    assert len(clients) == 1, f"expected one app client, got {len(clients)}"
    props = clients[0]["Properties"]
    assert props.get("GenerateSecret") in (False, None), (
        f"public client must not generate a secret, got "
        f"GenerateSecret={props.get('GenerateSecret')!r}"
    )
    callbacks = json.dumps(props.get("CallbackURLs", []))
    # The callback must be built from the distribution's own DomainName
    # attribute, not a hardcoded hostname -- that is what keeps the removed
    # custom domain (spec 007) from creeping back in.
    assert "SimulatorDistribution" in callbacks and "DomainName" in callbacks, (
        "callback URL must reference the CloudFront distribution's DomainName: "
        f"{callbacks}"
    )
    assert "/oauth2/idpresponse" in callbacks, (
        f"callback URL must be the Hosted UI callback path: {callbacks}"
    )


# ---------------------------------------------------------------------------
# Spec 029, Task 11.2 — CloudFront distribution on the default certificate
# ---------------------------------------------------------------------------


def test_cloudfront_default_certificate(raw_template: dict) -> None:
    """Spec 029 R1.2 / Task 11.2: exactly one CloudFront distribution using the
    default *.cloudfront.net certificate - no alternate domain names (Aliases)
    and no ACM certificate ARN. CDK expresses the default cert by omitting the
    ViewerCertificate block (or setting CloudFrontDefaultCertificate)."""
    dists = [
        v for v in raw_template["Resources"].values()
        if v["Type"] == "AWS::CloudFront::Distribution"
    ]
    assert len(dists) == 1, f"expected one CloudFront distribution, got {len(dists)}"
    dc = dists[0]["Properties"]["DistributionConfig"]
    assert not dc.get("Aliases"), f"distribution must have no Aliases, got {dc.get('Aliases')}"
    vc = dc.get("ViewerCertificate", {})
    assert "AcmCertificateArn" not in vc, "distribution must not reference an ACM certificate"
    # If a ViewerCertificate is present at all, it must be the default cert.
    if vc:
        assert vc.get("CloudFrontDefaultCertificate") is True, (
            f"ViewerCertificate present but not the default cert: {vc}"
        )


# ---------------------------------------------------------------------------
# Spec 029, Task 11.3 — viewer-request Lambda@Edge association
# ---------------------------------------------------------------------------


def test_viewer_request_edge_association(raw_template: dict) -> None:
    """Spec 029 R3.1 / Task 11.3: the distribution's default cache behavior has
    a Lambda@Edge association on the viewer-request event (the cognito-at-edge
    authenticator). Exercised because _synth() sets the edgeCognito* context."""
    dists = [
        v for v in raw_template["Resources"].values()
        if v["Type"] == "AWS::CloudFront::Distribution"
    ]
    assert len(dists) == 1
    behavior = dists[0]["Properties"]["DistributionConfig"]["DefaultCacheBehavior"]
    assocs = behavior.get("LambdaFunctionAssociations", [])
    events = [a.get("EventType") for a in assocs]
    assert "viewer-request" in events, (
        f"expected a viewer-request Lambda@Edge association, got {events}"
    )


# ---------------------------------------------------------------------------
# Spec 029 (Option A) — CSRF secret resolution: sidecar fallback + fail-loud
# ---------------------------------------------------------------------------
#
# Steady-state deploys pin the non-secret edgeCognito* IDs in cdk.json and pass
# no -c. The stack then reuses the CSRF secret already persisted in the
# gitignored config.local.json sidecar; if none exists it fails loud rather
# than baking an empty secret. See architecture-decisions.md (empty-default
# gate trap) and the spec-029 finding.


def _build_edge_app_no_secret(config_dir: str) -> cdk.App:
    """Build an App + stack with edge auth enabled (pinned IDs) but WITHOUT an
    ``edgeCsrfNonceSecret`` context value, sidecar redirected to ``config_dir``.
    The stack constructor runs here, so a fail-loud ValueError surfaces on this
    call (mirrors the chat-guard tests)."""
    app = cdk.App(context={
        "harnessArn": _harness_arn(),
        "iotEndpoint": _EXAMPLE_IOT_ENDPOINT,
        "chatFeatureRequired": False,
        "edgeCognitoUserPoolId": "us-west-2_TESTPOOL0",
        "edgeCognitoAppClientId": "testappclientid0000000000",
        "edgeCognitoUserPoolDomain": "restaurant-sim-123456789012.auth.us-west-2.amazoncognito.com",
        "edgeConfigDirOverride": config_dir,
    })
    SimulatorEc2AlbStack(
        app,
        "TestStack",
        env=cdk.Environment(region="us-west-2", account="123456789012"),
    )
    return app


def test_csrf_secret_reused_from_sidecar_when_context_absent(tmp_path) -> None:
    """Option A: edge auth enabled, no ``-c edgeCsrfNonceSecret``, but a sidecar
    secret already exists -> the stack reuses it (routine deploys need no -c).
    Synth succeeds, the edge function is associated, and the secret is preserved
    (not blanked) in the rewritten sidecar."""
    (tmp_path / "config.local.json").write_text(
        json.dumps({"csrfNonceSecret": "persisted-secret-value"}) + "\n",
        encoding="utf-8",
    )
    app = _build_edge_app_no_secret(str(tmp_path))
    raw = app.synth().get_stack_by_name("TestStack").template

    dists = [
        v for v in raw["Resources"].values()
        if v["Type"] == "AWS::CloudFront::Distribution"
    ]
    assert len(dists) == 1
    behavior = dists[0]["Properties"]["DistributionConfig"]["DefaultCacheBehavior"]
    events = [a.get("EventType") for a in behavior.get("LambdaFunctionAssociations", [])]
    assert "viewer-request" in events, "edge auth should be associated without -c"

    written = json.loads((tmp_path / "config.local.json").read_text(encoding="utf-8"))
    assert written["csrfNonceSecret"] == "persisted-secret-value"
    assert written["userPoolAppId"] == "testappclientid0000000000"


def test_csrf_secret_missing_everywhere_raises(tmp_path) -> None:
    """Option A fail-loud: edge auth enabled, no ``-c edgeCsrfNonceSecret`` and
    no sidecar to fall back to -> ValueError at synth (never bake an empty
    secret / silently break CSRF)."""
    with pytest.raises(ValueError) as excinfo:
        _build_edge_app_no_secret(str(tmp_path))  # empty tmp dir, no sidecar
    assert "edgeCsrfNonceSecret" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Spec 029, Task 11.4 — no ACM, no Route 53, no HTTPS listener
# ---------------------------------------------------------------------------


def test_no_acm_route53_or_https_listener(raw_template: dict) -> None:
    """Spec 029 R5.5 / Task 11.4: the domain stack is gone - no ACM certificate,
    no Route 53 resources, and no ALB listener on port 443/HTTPS."""
    types = {v["Type"] for v in raw_template["Resources"].values()}
    assert "AWS::CertificateManager::Certificate" not in types, "ACM certificate present"
    assert not any(t.startswith("AWS::Route53") for t in types), "Route 53 resource present"
    listeners = [
        v for v in raw_template["Resources"].values()
        if v["Type"] == "AWS::ElasticLoadBalancingV2::Listener"
    ]
    assert all(l["Properties"].get("Port") != 443 for l in listeners), "port 443 listener present"
    assert all(l["Properties"].get("Protocol") != "HTTPS" for l in listeners), "HTTPS listener present"


# ---------------------------------------------------------------------------
# Spec 029, Task 11.5 — internal ALB + CloudFront VPC origin
# ---------------------------------------------------------------------------


def test_alb_internal_with_vpc_origin(raw_template: dict) -> None:
    """Spec 029 R2.1 / Task 11.5: the ALB is internal (Scheme 'internal') and a
    CloudFront VPC origin exists to reach it."""
    albs = [
        v for v in raw_template["Resources"].values()
        if v["Type"] == "AWS::ElasticLoadBalancingV2::LoadBalancer"
    ]
    assert len(albs) == 1, f"expected one ALB, got {len(albs)}"
    assert albs[0]["Properties"].get("Scheme") == "internal", (
        f"ALB must be internal, got Scheme={albs[0]['Properties'].get('Scheme')!r}"
    )
    vpc_origins = [
        v for v in raw_template["Resources"].values()
        if v["Type"] == "AWS::CloudFront::VpcOrigin"
    ]
    assert len(vpc_origins) == 1, f"expected one CloudFront VpcOrigin, got {len(vpc_origins)}"


# ---------------------------------------------------------------------------
# Spec 029, Task 11.6 — no public (0.0.0.0/0) ingress on the ALB
# ---------------------------------------------------------------------------


def test_no_public_ingress(raw_template: dict) -> None:
    """Spec 029 R2.3 / Task 11.6: the ALB is reachable only from CloudFront's
    VPC-origin security group.

    Asserts the general invariant -- no ingress rule may carry a **hardcoded
    string** ``CidrIp`` -- rather than blocklisting specific values. This
    catches ``0.0.0.0/0`` and a developer's home IP pinned as a ``/32`` during
    debugging, including values nobody thought to add to a denylist. A CIDR
    supplied as a CloudFormation intrinsic (the VPC-endpoint SG references
    ``Fn::GetAtt SimulatorVpc.CidrBlock``) is legitimate and allowed, because it
    resolves to this VPC's own range rather than to an arbitrary address.
    """
    def _literal_cidr(value: object) -> str | None:
        # dict => CFN intrinsic (e.g. Fn::GetAtt VPC CidrBlock); str => literal.
        return value if isinstance(value, str) else None

    bad = []
    for logical_id, res in raw_template["Resources"].items():
        rtype = res["Type"]
        props = res.get("Properties", {}) or {}
        if rtype == "AWS::EC2::SecurityGroup":
            for ing in props.get("SecurityGroupIngress", []) or []:
                cidr = _literal_cidr(ing.get("CidrIp"))
                if cidr:
                    bad.append(
                        f"{logical_id}: CidrIp={cidr} "
                        f"{ing.get('FromPort')}-{ing.get('ToPort')}"
                    )
        if rtype == "AWS::EC2::SecurityGroupIngress":
            cidr = _literal_cidr(props.get("CidrIp"))
            if cidr:
                bad.append(
                    f"{logical_id}: CidrIp={cidr} "
                    f"{props.get('FromPort')}-{props.get('ToPort')}"
                )
    assert not bad, (
        "ingress must reference a security group (or an intra-VPC intrinsic), "
        f"not a hardcoded CIDR; found: {bad}"
    )


# ---------------------------------------------------------------------------
# Chat-feature context guard (follow-up from spec 016 takedown verdict)
# ---------------------------------------------------------------------------
#
# These tests cover the architecture-decisions.md lesson about CDK empty-
# default context traps: a bare cdk deploy without the chat context value
# would silently destroy the ChatMemoryAccess inline policy on the EC2 role.
#
# The guard used to key off an allowlist of AWS account ids, using account
# identity as a proxy for "this is a real deployment." It is now the explicit
# ``chatFeatureRequired`` context flag, which defaults to TRUE -- so the safe
# behaviour is the default, no account identifier appears in source, and the
# bootstrap case is an explicit opt-out rather than an implicit consequence of
# which account you happen to be deploying from.
#
# The module fixtures above opt out (``chatFeatureRequired`` false) so they
# exercise the "skip when empty" bootstrap branch. The tests below exercise
# the guard, the opt-out, and a populated-context synth.


_EXAMPLE_ACCOUNT: Final[str] = "123456789012"
_EXAMPLE_MEMORY_ID: Final[str] = "SellerAssistantMemory-EXAMPLE123"


def _synth_with_account(
    account: str,
    *,
    agentcore_memory_id: str | None = None,
    chat_feature_required: bool | str | None = None,
) -> cdk.App:
    """Helper: synth the stack with an optional chat-feature context override.

    Returns the App so tests can inspect the synthesized template or assert on
    raises. Edge auth is opted out here (this helper supplies no ``edgeCognito*``
    context); the edge guard has its own tests.
    """
    context: dict[str, object] = {
        "harnessArn": _harness_arn(account),
        "iotEndpoint": _EXAMPLE_IOT_ENDPOINT,
        "edgeAuthRequired": False,
    }
    if agentcore_memory_id is not None:
        context["agentcoreMemoryId"] = agentcore_memory_id
    if chat_feature_required is not None:
        context["chatFeatureRequired"] = chat_feature_required
    app = cdk.App(context=context)
    SimulatorEc2AlbStack(
        app,
        "TestStack",
        env=cdk.Environment(region="us-west-2", account=account),
    )
    return app


class TestChatFeatureContextGuard:
    """The guard raises ValueError when ``chatFeatureRequired`` is set (the
    default) and ``agentcoreMemoryId`` is empty. This prevents a bare
    ``cdk deploy`` from silently destroying the inline policy that gates the
    chat path.

    Spec 022 Task 6 removed the durable supervisor, so this guard no longer
    covers ``supervisorLambdaAliasArn`` (the key is gone) and there is no
    ``InvokeDurableSupervisor`` policy to protect.
    """

    def test_missing_memory_raises_by_default(self) -> None:
        """Memory context empty with the flag defaulted on -> ValueError."""
        with pytest.raises(ValueError) as excinfo:
            _synth_with_account(_EXAMPLE_ACCOUNT, agentcore_memory_id="")
        msg = str(excinfo.value)
        assert "agentcoreMemoryId" in msg
        assert "chatFeatureRequired" in msg

    def test_guard_is_on_by_default(self) -> None:
        """Omitting the key entirely still raises: the default must be SAFE.

        Regression guard for the empty-default-gate trap -- if the flag ever
        defaults to false, a bare deploy silently drops the live policy.
        """
        with pytest.raises(ValueError, match="agentcoreMemoryId"):
            _synth_with_account(_EXAMPLE_ACCOUNT)

    def test_explicit_opt_out_allows_empty_memory(self) -> None:
        """The documented bootstrap escape hatch skips the policy, not raises."""
        app = _synth_with_account(
            _EXAMPLE_ACCOUNT, agentcore_memory_id="", chat_feature_required=False
        )
        raw = app.synth().get_stack_by_name("TestStack").template
        assert not [
            lid
            for lid, res in raw.get("Resources", {}).items()
            if res.get("Type") == "AWS::IAM::Policy"
            and lid.startswith("ChatMemoryAccess")
        ], "opt-out should skip the ChatMemoryAccess policy"

    def test_opt_out_accepts_string_false_from_cli(self) -> None:
        """``-c chatFeatureRequired=false`` arrives as the STRING "false".

        A bare ``bool("false")`` is True, which would make the documented
        opt-out silently ineffective, so the flag parser must special-case it.
        """
        app = _synth_with_account(
            _EXAMPLE_ACCOUNT, agentcore_memory_id="", chat_feature_required="false"
        )
        assert app.synth().get_stack_by_name("TestStack").template

    def test_memory_present_synths_cleanly(self) -> None:
        """Memory context populated -> synth succeeds and the
        ChatMemoryAccess inline policy appears on the EC2 role."""
        app = _synth_with_account(
            _EXAMPLE_ACCOUNT, agentcore_memory_id=_EXAMPLE_MEMORY_ID
        )
        raw = app.synth().get_stack_by_name("TestStack").template

        logical_ids = {
            lid
            for lid, res in raw.get("Resources", {}).items()
            if res.get("Type") == "AWS::IAM::Policy"
        }
        assert any(lid.startswith("ChatMemoryAccess") for lid in logical_ids), (
            f"ChatMemoryAccess policy missing. Found IAM::Policy "
            f"logical ids: {sorted(logical_ids)}"
        )

    def test_durable_supervisor_policy_is_gone(self) -> None:
        """Regression guard: no InvokeDurableSupervisor policy may reappear.

        The durable supervisor was decommissioned in spec 022 Task 6; a
        reintroduced grant would hand the EC2 role permission on deleted
        infrastructure.
        """
        app = _synth_with_account(
            _EXAMPLE_ACCOUNT, agentcore_memory_id=_EXAMPLE_MEMORY_ID
        )
        raw = app.synth().get_stack_by_name("TestStack").template

        offenders = [
            lid
            for lid, res in raw.get("Resources", {}).items()
            if res.get("Type") == "AWS::IAM::Policy"
            and lid.startswith("InvokeDurableSupervisor")
        ]
        assert not offenders, f"durable-supervisor grant reappeared: {offenders}"

        # Belt and braces: no statement anywhere may grant the durable-
        # execution read actions.
        durable_actions = {
            "lambda:GetDurableExecution",
            "lambda:ListDurableExecutionsByFunction",
        }
        for lid, res in raw.get("Resources", {}).items():
            if res.get("Type") != "AWS::IAM::Policy":
                continue
            for stmt in res["Properties"]["PolicyDocument"]["Statement"]:
                actions = stmt["Action"]
                actions = actions if isinstance(actions, list) else [actions]
                assert not durable_actions.intersection(actions), (
                    f"{lid} grants durable-execution actions: {actions}"
                )

    def test_guard_does_not_depend_on_which_account(self) -> None:
        """The guard must behave identically on ANY account.

        It used to key off an allowlist of account ids, so a deploy from an
        unlisted account silently took the "skip when empty" branch -- safety
        depended on which credentials you happened to be using. Deploying from
        a different account must not be a way to disarm the guard, so an
        arbitrary account with empty memory context still raises.
        """
        for account in ("111111111111", "222222222222"):
            with pytest.raises(ValueError, match="agentcoreMemoryId"):
                _synth_with_account(account, agentcore_memory_id="")


class TestEdgeAuthRequiredGuard:
    """``edgeAuthRequired`` (default true) makes missing ``edgeCognito*``
    context a loud synth failure.

    Without it, an absent ``edgeCognitoAppClientId`` silently skips creating and
    associating the Lambda@Edge authenticator -- deploying a CloudFront
    distribution that serves the dashboard with NO authentication at all. That
    is the worst form of the empty-default-gate trap: the deploy succeeds and
    the regression is invisible until someone notices the site is open.
    """

    def _synth(self, **context: object) -> cdk.App:
        base: dict[str, object] = {
            "harnessArn": _harness_arn(),
            "iotEndpoint": _EXAMPLE_IOT_ENDPOINT,
            "chatFeatureRequired": False,
        }
        base.update(context)
        app = cdk.App(context=base)
        SimulatorEc2AlbStack(
            app,
            "TestStack",
            env=cdk.Environment(region="us-west-2", account="123456789012"),
        )
        return app

    def test_missing_edge_cognito_raises_by_default(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            self._synth()
        msg = str(excinfo.value)
        assert "edgeCognitoAppClientId" in msg
        assert "edgeAuthRequired" in msg

    def test_explicit_opt_out_allows_bootstrap(self) -> None:
        """Phase 1 of the two-phase bootstrap: the pool does not exist yet."""
        app = self._synth(edgeAuthRequired=False)
        assert app.synth().get_stack_by_name("TestStack").template

    def test_opt_out_accepts_string_false_from_cli(self) -> None:
        """``-c edgeAuthRequired=false`` arrives as the STRING "false"."""
        app = self._synth(edgeAuthRequired="false")
        assert app.synth().get_stack_by_name("TestStack").template


class TestArnAccountValidation:
    """``_validate_arn_account`` rejects ARNs whose account segment
    doesn't match the deploy account. This catches the ``000000000000``
    placeholder trap documented in architecture-decisions.md.

    Applied to ``harnessArn`` since spec 022 Task 6 (previously it guarded
    ``supervisorLambdaAliasArn``, which no longer exists).
    """

    def test_mismatched_account_raises(self) -> None:
        """Harness ARN with wrong account -> ValueError at synth time."""
        wrong_account_arn = (
            "arn:aws:bedrock-agentcore:us-west-2:000000000000:harness/h-1"
        )
        app = cdk.App(
            context={
                "harnessArn": wrong_account_arn,
                "agentcoreMemoryId": _EXAMPLE_MEMORY_ID,
            }
        )
        with pytest.raises(ValueError) as excinfo:
            SimulatorEc2AlbStack(
                app,
                "TestStack",
                env=cdk.Environment(region="us-west-2", account=_EXAMPLE_ACCOUNT),
            )
        msg = str(excinfo.value)
        assert "000000000000" in msg
        assert _EXAMPLE_ACCOUNT in msg
        assert "harnessArn" in msg

    def test_empty_harness_arn_raises(self) -> None:
        """An empty ``harnessArn`` must fail loudly, not render blank.

        The harness is the only chat backend, so an empty-default gate here
        would deploy a simulator whose chat path returns 503
        (architecture-decisions.md § "CDK stack context flags: empty-default
        gates are traps at deploy time").
        """
        app = cdk.App(context={"agentcoreMemoryId": _EXAMPLE_MEMORY_ID})
        with pytest.raises(ValueError) as excinfo:
            SimulatorEc2AlbStack(
                app,
                "TestStack",
                env=cdk.Environment(region="us-west-2", account=_EXAMPLE_ACCOUNT),
            )
        assert "harnessArn" in str(excinfo.value)



# ---------------------------------------------------------------------------
# UserData-causes-replacement guard (follow-up from 2026-05-05 amendment to
# architecture-decisions.md § "EC2 user-data bundles and stale deployment
# state").
# ---------------------------------------------------------------------------
#
# Without ``user_data_causes_replacement=True`` on ``ec2.Instance(...)``, CDK
# renders UserData as a property-level update: CloudFormation stops the
# instance, rewrites its launch metadata, and starts it again, but cloud-
# init's per-instance sentinel prevents the user-data script from re-
# running. Net effect: the repo-asset hash updates in UserData but the
# instance file system never sees the new bundle. The flag makes CDK fold
# a UserData hash into the instance's logical id, so any UserData change
# becomes a full replacement and the fresh instance bootstraps from
# scratch.
#
# This test is a regression guard. With the flag off, the logical id is
# the stable "SimulatorInstanceF3C55FDD". With the flag on, CDK appends
# an 8-hex-character suffix derived from the UserData hash.


def test_instance_logical_id_carries_user_data_hash(raw_template: dict) -> None:
    """R-new: UserData changes MUST force an EC2 instance replacement.

    Regression guard for the 2026-05-05 discovery that removing
    ``user_data_causes_replacement=True`` from the SimulatorInstance
    construct silently converts UserData changes into metadata-only
    updates. The architecture-decisions.md entry "EC2 user-data bundles
    and stale deployment state" covers the full diagnosis.
    """
    instances = [
        (logical_id, resource)
        for logical_id, resource in raw_template["Resources"].items()
        if resource["Type"] == "AWS::EC2::Instance"
    ]
    assert len(instances) == 1, (
        f"expected exactly one AWS::EC2::Instance, got {len(instances)}"
    )
    logical_id, _ = instances[0]
    # Flag OFF: "SimulatorInstanceF3C55FDD" (exact, 8 chars of hash suffix
    # on the construct id).
    # Flag ON:  "SimulatorInstanceF3C55FDD<hex>" with an additional hash
    # segment folded in from the UserData content.
    match = re.match(r"^SimulatorInstance([A-Fa-f0-9]+)$", logical_id)
    assert match is not None, (
        f"EC2 Instance logical id {logical_id!r} does not start with "
        "'SimulatorInstance' followed by hex characters."
    )
    suffix = match.group(1)
    # The base construct-id hash is 8 hex chars. With user-data hashing
    # enabled, CDK appends a separate content-hash segment, pushing the
    # total hex-suffix length to at least 16.
    assert len(suffix) >= 16, (
        f"EC2 Instance logical id suffix {suffix!r} (length {len(suffix)}) "
        "looks too short — is user_data_causes_replacement=True still "
        "set on the SimulatorInstance construct? Without it, CDK does "
        "not fold the UserData hash into the logical id and UserData "
        "changes become metadata-only updates that cloud-init silently "
        "skips on the next boot."
    )


# ---------------------------------------------------------------------------
# Spec 022 Task 4 — harness-backend caller grants + SP2 security boundary
# ---------------------------------------------------------------------------


def _statements_of(resource: dict) -> list:
    """Policy statements on an AWS::IAM::Policy or inline on an AWS::IAM::Role."""
    props = resource.get("Properties", {})
    out = list((props.get("PolicyDocument") or {}).get("Statement") or [])
    for p in props.get("Policies") or []:
        out += (p.get("PolicyDocument") or {}).get("Statement") or []
    return out


def _all_policy_statements(raw: dict) -> list[dict]:
    """Collect every IAM policy statement in the template: standalone
    ``AWS::IAM::Policy`` resources (attach_inline_policy) plus inline
    ``Policies`` embedded on ``AWS::IAM::Role`` resources."""
    statements: list[dict] = []
    for res in raw.get("Resources", {}).values():
        rtype = res.get("Type")
        props = res.get("Properties", {})
        if rtype == "AWS::IAM::Policy":
            statements.extend(props.get("PolicyDocument", {}).get("Statement", []))
        elif rtype == "AWS::IAM::Role":
            for pol in props.get("Policies", []):
                statements.extend(pol.get("PolicyDocument", {}).get("Statement", []))
    return statements


def _actions(stmt: dict) -> list[str]:
    a = stmt.get("Action", [])
    return [a] if isinstance(a, str) else list(a)


def _resources(stmt: dict) -> list:
    r = stmt.get("Resource", [])
    return [r] if not isinstance(r, list) else r


class TestHarnessBackendGrants:
    """Spec 022 Task 4: the EC2 role carries least-privilege harness grants."""

    def test_invoke_harness_scoped_to_name_prefix(self, raw_template: dict) -> None:
        # GA invoke_harness requires BOTH InvokeHarness and InvokeAgentRuntime
        # on the harness ARN (granting only one fails at invoke time).
        for action in ("bedrock-agentcore:InvokeHarness", "bedrock-agentcore:InvokeAgentRuntime"):
            stmts = [s for s in _all_policy_statements(raw_template) if action in _actions(s)]
            assert stmts, f"{action} grant missing from the EC2 role"
            for s in stmts:
                for r in _resources(s):
                    assert isinstance(r, str)
                    assert r != "*"
                    assert r.endswith(":harness/restaurant_manager_agent-*"), r

    def test_invoke_agent_runtime_only_on_harness(self, raw_template: dict) -> None:
        """InvokeAgentRuntime is allowed, but ONLY scoped to this project's
        harness resource -- never Resource '*' and never a bare runtime ARN
        (so the role cannot invoke arbitrary AgentCore runtimes)."""
        for s in _all_policy_statements(raw_template):
            if "bedrock-agentcore:InvokeAgentRuntime" in _actions(s):
                for r in _resources(s):
                    assert isinstance(r, str) and ":harness/restaurant_manager_agent-" in r and r != "*", r

    def test_iot_round_trip_actions_scoped(self, raw_template: dict) -> None:
        by_action: dict[str, list] = {}
        for s in _all_policy_statements(raw_template):
            for a in _actions(s):
                if a.startswith("iot:"):
                    by_action.setdefault(a, []).extend(_resources(s))

        # Exactly the four outbound round-trip actions, nothing else.
        assert set(by_action) == {"iot:Connect", "iot:Publish", "iot:Subscribe", "iot:Receive"}, by_action

        # Publish only to command topics; subscribe/receive only to responses.
        assert all("/commands/" in r for r in by_action["iot:Publish"])
        assert all("/responses/" in r for r in by_action["iot:Subscribe"])
        assert all("/responses/" in r for r in by_action["iot:Receive"])
        assert all(":client/chat-harness-*" in r for r in by_action["iot:Connect"])
        # Never a blanket iot:* or Resource "*".
        for resources in by_action.values():
            assert all(r != "*" for r in resources)

    def test_iot_grants_scoped_to_this_host_store_not_the_fleet(
        self, raw_template: dict
    ) -> None:
        """TM-01: IoT topic grants must name THIS store, never `stores/*`.

        The fleet-wide wildcard was the missing compensating control behind the
        cross-tenant finding: application logic routed a tool call using a
        model-supplied `store_id`, and IAM was happy to publish it anywhere in
        the fleet. Pinning the topic is the only one of TM-01's three controls
        that holds even if the application is wrong again, so it gets its own
        test rather than being folded into the scoping test above.
        """
        store_id = json.loads(
            (Path(__file__).resolve().parents[1] / "cdk.json").read_text(
                encoding="utf-8"
            )
        )["context"]["storeId"]

        topic_resources = [
            r
            for s in _all_policy_statements(raw_template)
            for a in _actions(s)
            if a in ("iot:Publish", "iot:Subscribe", "iot:Receive")
            for r in _resources(s)
        ]
        assert topic_resources, "expected IoT topic grants on the EC2 role"
        for r in topic_resources:
            assert "stores/*" not in r, (
                f"IoT grant {r!r} is scoped to the whole fleet. A model-chosen "
                "store_id would then be publishable cross-tenant (TM-01)."
            )
            assert f"stores/{store_id}/" in r, (
                f"IoT grant {r!r} does not name this host's store "
                f"({store_id!r})"
            )

    def test_cdk_store_id_matches_user_data(self) -> None:
        """The IAM scope and the running app must agree on the store.

        The IAM grant is built from `cdk.json`'s `storeId`, while the app reads
        `STORE_ID` exported by `user_data.sh`. If they drift, synth and deploy
        both succeed and every tool call is simply denied by IoT at runtime --
        so this is asserted rather than left to a comment. Same class of trap as
        the harness `harnessName` and the web-push bundle-image/runtime pair.
        """
        root = Path(__file__).resolve().parents[1]
        store_id = json.loads((root / "cdk.json").read_text(encoding="utf-8"))[
            "context"
        ]["storeId"]
        user_data = (root / "scripts" / "user_data.sh").read_text(encoding="utf-8")
        match = re.search(r"^STORE_ID=(\S+)", user_data, re.MULTILINE)
        assert match, "could not find STORE_ID= in user_data.sh"
        assert match.group(1) == store_id, (
            f"user_data.sh exports STORE_ID={match.group(1)!r} but cdk.json "
            f"scopes IAM to {store_id!r}; every tool call would be denied"
        )

    def test_sp2_no_inbound_store_actions(self, raw_template: dict) -> None:
        """SP2: the backend role grants no action that could create an inbound
        path into the store (thing/rule/policy management or a topic rule)."""
        forbidden = (
            "iot:CreateThing", "iot:CreateTopicRule", "iot:ReplaceTopicRule",
            "iot:CreatePolicy", "iot:AttachPolicy", "iot:CreateKeysAndCertificate",
            "iot:UpdateEventConfigurations", "iot:*",
        )
        all_actions = {a for s in _all_policy_statements(raw_template) for a in _actions(s)}
        for bad in forbidden:
            assert bad not in all_actions, f"SP2 violation: {bad} granted"

    def test_no_wildcard_or_iam_actions_on_ec2_role(self, raw_template: dict) -> None:
        """No `Action: "*"` and no `iam:*` anywhere in the stack.

        Note the scope is STACK-WIDE despite the name -- `_all_policy_statements`
        walks every policy, not just the EC2 role's. That is the more valuable
        invariant, so it is kept rather than narrowed to match the name.

        One exemption: enabling VPC flow logs (cdk-nag AwsSolutions-VPC7) makes
        CDK synthesize a delivery role whose whole purpose is to be passed to the
        flow-log service, so it carries `iam:PassRole` on itself. That is a
        CDK-generated construct, the action is the sole action in its statement,
        and it cannot be used to escalate into another principal -- so it is
        allowed by logical id rather than by relaxing the rule.
        """
        _FLOW_LOG_ROLE_ID = "cloudwatchIAMRole"  # CDK's VPC flow-log delivery role
        for logical_id, policy in raw_template["Resources"].items():
            if policy.get("Type") not in ("AWS::IAM::Policy", "AWS::IAM::Role"):
                continue
            for s in _statements_of(policy):
                for a in _actions(s):
                    assert a != "*", f"{logical_id}: Action '*'"
                    if a == "iam:PassRole" and _FLOW_LOG_ROLE_ID in logical_id:
                        continue
                    assert not a.startswith("iam:"), f"{logical_id}: {a}"
