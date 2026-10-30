# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Fleet-scalable IoT policy document (spec 020 R1.3 = A).

A single policy template scopes every store to its own topic space via the
``${iot:Connection.Thing.ThingName}`` policy variable. It resolves only when
the connecting cert is attached to exactly one thing AND the MQTT client id
equals the thing name -- which holds because the simulator now sets
``client_id == store_id == thing_name`` (R1.3 A; see simulator/mqtt/config.py).

Pure function so it is unit-testable without AWS.
"""
from __future__ import annotations

import json

POLICY_NAME = "RestaurantStoreIotPolicy"
_TV = "${iot:Connection.Thing.ThingName}"


def build_policy_document(region: str, account: str) -> dict:
    base = f"arn:aws:iot:{region}:{account}"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": "iot:Connect",
             "Resource": f"{base}:client/{_TV}"},
            {"Effect": "Allow", "Action": "iot:Subscribe",
             "Resource": [
                 f"{base}:topicfilter/stores/{_TV}/commands/*",
                 # spec 023: receive cloud-approved proactive notifications.
                 f"{base}:topicfilter/stores/{_TV}/notifications/*",
             ]},
            {"Effect": "Allow", "Action": "iot:Receive",
             "Resource": [
                 f"{base}:topic/stores/{_TV}/commands/*",
                 f"{base}:topic/stores/{_TV}/notifications/*",
             ]},
            {"Effect": "Allow", "Action": "iot:Publish",
             "Resource": [
                 f"{base}:topic/stores/{_TV}/responses/*",
                 # spec 023: publish alert + ack events (stores/<id>/events/{domain|ack}).
                 f"{base}:topic/stores/{_TV}/events/*",
                 # spec 025 Task 1d: relay Web Push subscriptions + (display-offline)
                 # re-engagement requests. Outside events/ so the spec-023 ingest
                 # rule does not match them.
                 f"{base}:topic/stores/{_TV}/push-subscriptions",
                 f"{base}:topic/stores/{_TV}/push-requests/*",
             ]},
        ],
    }


def build_policy_json(region: str, account: str) -> str:
    return json.dumps(build_policy_document(region, account))
