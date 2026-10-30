# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Proactive alert ingest handler (spec 023 Task 4).

Triggered by the IoT Topic Rule on ``stores/+/events/+`` (excluding
``events/ack``). For each store alert event:

1. **Validate** the envelope (required fields + severity enum) *and* the event's
   **authenticity** -- the store id is taken from the IoT-authenticated topic,
   not the self-declared payload, and a payload/topic mismatch (or a missing
   topic) is rejected. Invalid events are routed to the events DLQ and dropped
   -- the handler never throws past this point, so a malformed or spoofed event
   cannot poison the pipeline.
2. **Dedup + persist** with an *atomic conditional write*. The item key is
   deterministic per ``(domain, source_id, condition)`` within a time *bucket*
   (``now // DEDUP_WINDOW_SECONDS``), so re-fires within the window -- and QoS-1
   redeliveries of the same event -- collapse onto the same row. The first write
   in a bucket wins (``attribute_not_exists(SK)``); a losing write is a dedup
   hit (bump ``last_seen_at`` / ``fire_count``, no re-emit).
3. **Emit** ``AlertRaised`` to EventBridge only on a genuinely new alert.

This atomic primary-key approach supersedes the GSI-query + sliding-window
sketch in design.md (it is race-free and needs no eventual-consistent GSI read).
The ``DedupIndex`` GSI remains for active-alert lookups by dedup_key.
"""
from __future__ import annotations

import json
import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ALERTS_TABLE_NAME = os.environ.get("ALERTS_TABLE_NAME", "Alerts")
DEDUP_INDEX_NAME = os.environ.get("DEDUP_INDEX_NAME", "DedupIndex")
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "proactive-alerts")
EVENTS_DLQ_URL = os.environ.get("EVENTS_DLQ_URL", "")
DEDUP_WINDOW_SECONDS = int(os.environ.get("DEDUP_WINDOW_MINUTES", "15")) * 60
ALERTS_TTL_SECONDS = int(os.environ.get("ALERTS_TTL_DAYS", "30")) * 86400

ALERT_EVENT_SOURCE = "proactive.alerts"
ALERT_RAISED_DETAIL_TYPE = "AlertRaised"
_REQUIRED = (
    "schema_version", "event_id", "store_id", "domain", "event_type",
    "severity", "source_id", "condition", "timestamp", "dedup_key",
)
_SEVERITIES = {"info", "warning", "critical"}

_ddb = boto3.resource("dynamodb")
_table = _ddb.Table(ALERTS_TABLE_NAME)
_events = boto3.client("events")
_sqs = boto3.client("sqs")


def validate_envelope(event) -> str | None:
    """Return a failure reason string, or None if the envelope is valid."""
    if not isinstance(event, dict):
        return "not_a_dict"
    missing = [k for k in _REQUIRED if not event.get(k)]
    if missing:
        return "missing_fields:" + ",".join(missing)
    if event["severity"] not in _SEVERITIES:
        return "bad_severity:" + str(event["severity"])
    return None


def topic_store_id(event) -> str | None:
    """Store id from the IoT-authenticated topic, or None if absent/malformed.

    The ingest Topic Rule selects ``topic() AS topic``, so ``event["topic"]`` is
    ``stores/{store_id}/events/{domain}``. This is the *authenticated* store
    identity: the shared fleet thing-policy binds each device to
    ``stores/{its-own-thing}/events/*`` (via ``${iot:Connection.Thing.ThingName}``),
    so the topic segment cannot be forged. The payload ``store_id`` field is
    self-declared and MUST be checked against this (see handler).
    """
    topic = event.get("topic") if isinstance(event, dict) else None
    if not isinstance(topic, str):
        return None
    parts = topic.split("/")
    # stores/{store_id}/events/{domain}
    if len(parts) < 4 or parts[0] != "stores" or parts[2] != "events" or not parts[1]:
        return None
    return parts[1]


def _now() -> int:
    return int(time.time())


def _window_bucket(now: int) -> int:
    return now // DEDUP_WINDOW_SECONDS if DEDUP_WINDOW_SECONDS > 0 else now


# --- AWS calls (thin wrappers; monkeypatched in unit tests) ---

def _put_new_alert(item: dict) -> None:
    _table.put_item(Item=item, ConditionExpression="attribute_not_exists(SK)")


def _bump_last_seen(pk: str, sk: str, now: int) -> None:
    _table.update_item(
        Key={"PK": pk, "SK": sk},
        UpdateExpression="SET last_seen_at = :n ADD fire_count :one",
        ExpressionAttributeValues={":n": now, ":one": 1},
    )


def _emit_alert_raised(item: dict) -> None:
    detail = {
        "alert_id": item["alert_id"],
        "store_id": item["PK"].split("STORE#", 1)[-1],
        "domain": item["domain"],
        "severity": item["severity"],
        "source_id": item["source_id"],
        "condition": item["condition"],
        "dedup_key": item["dedup_key"],
        "created_at": item["created_at"],
        "envelope": json.loads(item["envelope_json"]),
    }
    _events.put_events(Entries=[{
        "Source": ALERT_EVENT_SOURCE,
        "DetailType": ALERT_RAISED_DETAIL_TYPE,
        "Detail": json.dumps(detail),
        "EventBusName": EVENT_BUS_NAME,
    }])


def _send_to_dlq(event, reason: str) -> None:
    if not EVENTS_DLQ_URL:
        logger.warning("no events DLQ configured; dropping invalid event reason=%s", reason)
        return
    try:
        _sqs.send_message(
            QueueUrl=EVENTS_DLQ_URL,
            MessageBody=json.dumps({"reason": reason, "event": event}, default=str),
        )
    except Exception:  # noqa: BLE001 - DLQ send is best-effort
        logger.warning("events DLQ send failed reason=%s", reason, exc_info=True)


def handler(event: dict, _context) -> dict:
    reason = validate_envelope(event)
    if reason:
        _send_to_dlq(event, reason)
        print(json.dumps({"marker": "alert_ingest_invalid", "reason": reason}))
        return {"status": "invalid", "reason": reason}

    # Event authenticity: the trusted store identity is the IoT-authenticated
    # topic, NOT the self-declared payload field. The fleet thing-policy lets a
    # device publish only to stores/{its-own-thing}/events/*, but nothing stops
    # it putting a *different* store_id in the body -- which would attribute the
    # alert to (and deliver notifications to) another store. Derive the id from
    # the topic and reject a mismatch. (SAX-04: disallow untrusted data; trust
    # the authenticated channel, not a self-reported field.)
    store_id = topic_store_id(event)
    if store_id is None:
        _send_to_dlq(event, "missing_or_bad_topic")
        print(json.dumps({"marker": "alert_ingest_invalid", "reason": "missing_or_bad_topic"}))
        return {"status": "invalid", "reason": "missing_or_bad_topic"}
    if store_id != event["store_id"]:
        reason = f"store_id_mismatch:topic={store_id},payload={event['store_id']}"
        _send_to_dlq(event, reason)
        print(json.dumps({"marker": "alert_ingest_store_mismatch", "reason": reason}))
        return {"status": "invalid", "reason": reason}

    dedup_key = event["dedup_key"]
    now = _now()
    bucket = _window_bucket(now)
    alert_id = f"{event['domain']}-{event['source_id']}-{event['condition']}-{bucket}"
    pk = f"STORE#{store_id}"
    sk = f"ALERT#{alert_id}"
    item = {
        "PK": pk,
        "SK": sk,
        "alert_id": alert_id,
        "status": "active",
        "dedup_key": dedup_key,
        "severity": event["severity"],
        "domain": event["domain"],
        "source_id": event["source_id"],
        "condition": event["condition"],
        "envelope_json": json.dumps(event, default=str),
        "created_at": now,
        "last_seen_at": now,
        "fire_count": 1,
        "ttl": now + ALERTS_TTL_SECONDS,
    }

    try:
        _put_new_alert(item)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            # Re-fire within the window (or QoS-1 redelivery): collapse onto the
            # existing alert; do not re-emit.
            _bump_last_seen(pk, sk, now)
            print(json.dumps({"marker": "alert_ingest_dedup", "alert_id": alert_id}))
            return {"status": "deduplicated", "alert_id": alert_id}
        raise

    _emit_alert_raised(item)
    print(json.dumps({
        "marker": "alert_ingest_raised", "alert_id": alert_id, "store_id": store_id,
    }))
    return {"status": "raised", "alert_id": alert_id}
