# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Proactive alert acknowledgment handler (spec 023 Task 7).

Triggered by the IoT Topic Rule on ``stores/+/events/ack``. Resolves the waiting
Step Functions execution's task token (persisted on the Alerts row by Notify)
and calls ``SendTaskSuccess`` to resume it, then marks the alert acknowledged.
Idempotent: a missing or already-consumed token is a benign no-op.
"""
from __future__ import annotations

import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ALERTS_TABLE_NAME = os.environ.get("ALERTS_TABLE_NAME", "Alerts")

_table = boto3.resource("dynamodb").Table(ALERTS_TABLE_NAME)
_sfn = boto3.client("stepfunctions")

# SendTaskSuccess errors that mean "the wait is already gone" -> benign.
_BENIGN = {"TaskDoesNotExist", "TaskTimedOut", "InvalidToken"}


def _store_id_from_topic(topic: str) -> str | None:
    parts = topic.split("/")
    if len(parts) == 4 and parts[0] == "stores" and parts[2] == "events":
        return parts[1]
    return None


def _get_alert(store_id: str, alert_id: str) -> dict | None:
    return _table.get_item(
        Key={"PK": f"STORE#{store_id}", "SK": f"ALERT#{alert_id}"}
    ).get("Item")


def _send_task_success(task_token: str, result: dict) -> None:
    _sfn.send_task_success(taskToken=task_token, output=json.dumps(result))


def _mark_acknowledged(store_id: str, alert_id: str) -> None:
    _table.update_item(
        Key={"PK": f"STORE#{store_id}", "SK": f"ALERT#{alert_id}"},
        UpdateExpression="SET #s = :a REMOVE task_token",
        ExpressionAttributeNames={"#s": "status"},  # 'status' is a DynamoDB reserved word
        ExpressionAttributeValues={":a": "acknowledged"},
    )


def handler(event: dict, _context) -> dict:
    topic = event.get("topic", "")
    # Event authenticity: the trusted store identity is the IoT-authenticated
    # topic, not the self-declared payload. A body store_id that disagrees with
    # the topic is a cross-tenant ack attempt (acking/suppressing another
    # store's alert) and is rejected. (SAX-04: trust the authenticated channel.)
    topic_store = _store_id_from_topic(topic)
    payload_store = event.get("store_id")
    if payload_store and topic_store and payload_store != topic_store:
        print(json.dumps({"marker": "alert_ack_store_mismatch",
                          "topic_store": topic_store, "payload_store": payload_store}))
        return {"status": "dropped", "reason": "store_id_mismatch"}
    store_id = topic_store or payload_store
    alert_id = event.get("alert_id")
    print(json.dumps({"marker": "alert_ack_entry", "store_id": store_id, "alert_id": alert_id}))
    if not store_id or not alert_id:
        return {"status": "dropped", "reason": "missing_ids"}

    item = _get_alert(store_id, alert_id)
    token = (item or {}).get("task_token")
    if not token:
        # Unknown alert, or already acknowledged/expired (token consumed).
        print(json.dumps({"marker": "alert_ack_no_token", "alert_id": alert_id}))
        return {"status": "no_token", "alert_id": alert_id}

    try:
        _send_task_success(token, {"acknowledged": True, "alert_id": alert_id})
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code not in _BENIGN:
            raise
        print(json.dumps({"marker": "alert_ack_benign", "alert_id": alert_id, "code": code}))

    _mark_acknowledged(store_id, alert_id)
    print(json.dumps({"marker": "alert_ack_resumed", "alert_id": alert_id}))
    return {"status": "acknowledged", "alert_id": alert_id}
