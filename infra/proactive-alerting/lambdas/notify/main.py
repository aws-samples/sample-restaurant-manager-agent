# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Notify publish (spec 023 Task 5).

Invoked by the ``Notify`` Step Functions state with the policy-evaluation output.
Publishes the approved notification to the store's notifications topic
(``stores/{store_id}/notifications/alert``, QoS 1) over the connection-less
``iot-data`` data plane, so the in-store web app can render (and, in Phase 1,
speak) it. The store receives it on its outbound mTLS subscription.
"""
from __future__ import annotations

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

NOTIFICATIONS_KIND = os.environ.get("NOTIFICATIONS_KIND", "alert")
ALERTS_TABLE_NAME = os.environ.get("ALERTS_TABLE_NAME", "Alerts")

_iot = boto3.client("iot-data")
_table = boto3.resource("dynamodb").Table(ALERTS_TABLE_NAME)


def _persist_task_token(store_id: str, alert_id: str, task_token: str) -> None:
    """Persist the Step Functions task token on the alert row so the ack handler
    can resume the waiting execution by alert_id (spec 023 Task 7)."""
    _table.update_item(
        Key={"PK": f"STORE#{store_id}", "SK": f"ALERT#{alert_id}"},
        UpdateExpression="SET task_token = :t",
        ExpressionAttributeValues={":t": task_token},
    )


def handler(event: dict, _context) -> dict:
    store_id = event["store_id"]
    alert_id = event.get("alert_id")
    notification = event["notification"]
    topic = f"stores/{store_id}/notifications/{NOTIFICATIONS_KIND}"
    _iot.publish(topic=topic, qos=1, payload=json.dumps(notification).encode("utf-8"))

    # When invoked as a waitForTaskToken task, persist the token so the ack
    # handler can resume this execution; the state machine stays paused until then.
    task_token = event.get("task_token")
    if task_token and alert_id:
        _persist_task_token(store_id, alert_id, task_token)

    print(json.dumps({
        "marker": "notify_published",
        "store_id": store_id,
        "alert_id": alert_id,
        "topic": topic,
        "awaiting_ack": bool(task_token),
    }))
    return {"published": True, "topic": topic, "alert_id": alert_id}
