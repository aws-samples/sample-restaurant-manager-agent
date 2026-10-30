# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the fleet IoT policy builder (spec 020 Task 2, R1.3=A)."""
from __future__ import annotations

from policy import build_policy_document

_TV = "${iot:Connection.Thing.ThingName}"


def _resources(stmt) -> list[str]:
    r = stmt["Resource"]
    return r if isinstance(r, list) else [r]


def _by_action(doc) -> dict:
    out: dict[str, list[str]] = {}
    for s in doc["Statement"]:
        out.setdefault(s["Action"], []).extend(_resources(s))
    return out


def test_policy_uses_thing_variable_for_every_resource():
    doc = build_policy_document("us-west-2", "123456789012")
    for stmt in doc["Statement"]:
        for r in _resources(stmt):
            assert _TV in r, f"{stmt['Action']} resource not thing-scoped: {r}"


def test_policy_scopes_connect_commands_responses():
    by_action = _by_action(build_policy_document("us-west-2", "123456789012"))
    assert any(r.endswith(f"client/{_TV}") for r in by_action["iot:Connect"])
    assert any(f"topicfilter/stores/{_TV}/commands/*" in r for r in by_action["iot:Subscribe"])
    assert any(f"topic/stores/{_TV}/commands/*" in r for r in by_action["iot:Receive"])
    assert any(f"topic/stores/{_TV}/responses/*" in r for r in by_action["iot:Publish"])


def test_policy_grants_events_publish_and_notifications_subscribe_receive():
    # spec 023 Task 2: additive proactive-alerting grants.
    by_action = _by_action(build_policy_document("us-west-2", "123456789012"))
    assert any(f"topic/stores/{_TV}/events/*" in r for r in by_action["iot:Publish"]), \
        "store must publish alert/ack events"
    assert any(
        f"topicfilter/stores/{_TV}/notifications/*" in r for r in by_action["iot:Subscribe"]
    ), "store must subscribe to notifications"
    assert any(
        f"topic/stores/{_TV}/notifications/*" in r for r in by_action["iot:Receive"]
    ), "store must receive notifications"


def test_policy_does_not_grant_cross_store_or_wildcard_topics():
    doc = build_policy_document("us-west-2", "123456789012")
    for stmt in doc["Statement"]:
        for r in _resources(stmt):
            # No bare wildcard store segment that would allow another store's topics.
            assert "stores/*/" not in r


def test_policy_grants_push_subscription_and_request_publish():
    # spec 025 Task 1d: the store relays Web Push subscriptions + (display-offline)
    # re-engagement requests over its outbound IoT connection.
    by_action = _by_action(build_policy_document("us-west-2", "123456789012"))
    assert any(
        f"topic/stores/{_TV}/push-subscriptions" in r for r in by_action["iot:Publish"]
    ), "store must publish push subscriptions"
    assert any(
        f"topic/stores/{_TV}/push-requests/*" in r for r in by_action["iot:Publish"]
    ), "store must publish push re-engagement requests"
