# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Web Push sender (spec 025 Task 1d).

Triggered by the IoT Topic Rule on ``stores/+/push-requests/+``. The store backend
publishes a re-engagement request here ONLY when the always-on display is not
actively connected (the store owns the connectivity signal); the connected path
speaks the alert and never reaches here. This handler sends a VAPID Web Push to
every registered subscription for the store so a backgrounded/closed display is
woken -- clicking the notification focuses the display, which reconnects and
speaks the alert (spec 025 R3/R4).

The VAPID private key + subject come from Secrets Manager (scoped to this Lambda);
403 (VAPID mismatch after rotation) / 404 / 410 (Gone) subscriptions are pruned.
``pywebpush`` is imported lazily so unit tests can mock the send without the native
dependency installed.

The store id is taken ONLY from the IoT-authenticated topic, never the
self-declared payload; a payload/topic mismatch (or a missing topic) is dropped
-- the same rule as the alert ingest handler (threat model T2 / M15).
"""
from __future__ import annotations

import json
import logging
import os

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SUBSCRIPTIONS_TABLE_NAME = os.environ.get("SUBSCRIPTIONS_TABLE_NAME", "PushSubscriptions")
VAPID_SECRET_ARN = os.environ.get("VAPID_SECRET_ARN", "")
# 404/410 = subscription gone; 403 = VAPID public-key mismatch after a key
# rotation (the sub was made against the old key) -> prune it too (spec 028).
GONE_STATUS = {403, 404, 410}

_table = boto3.resource("dynamodb").Table(SUBSCRIPTIONS_TABLE_NAME)
_secrets = boto3.client("secretsmanager")


def _store_id_from_topic(topic: str) -> str | None:
    parts = topic.split("/")
    # stores/{store_id}/push-requests/{alert_id}
    if len(parts) == 4 and parts[0] == "stores" and parts[2] == "push-requests":
        return parts[1]
    return None


def _load_vapid() -> dict:
    """Fetch the VAPID {private_key, subject} from Secrets Manager.

    Loaded per invocation (no process-level cache): a push-request right after a
    key rotation must sign with the CURRENT key. An lru_cache would pin a
    rotated-out key on a warm container, breaking every send until recycle
    (spec 028). Push events are rare, so one GetSecretValue per event is cheap."""
    resp = _secrets.get_secret_value(SecretId=VAPID_SECRET_ARN)
    return json.loads(resp["SecretString"])


def _query_subscriptions(store_id: str) -> list[dict]:
    resp = _table.query(
        KeyConditionExpression=Key("PK").eq(f"STORE#{store_id}") & Key("SK").begins_with("SUB#")
    )
    return resp.get("Items", [])


def _delete_subscription(store_id: str, sub_id: str) -> None:
    _table.delete_item(Key={"PK": f"STORE#{store_id}", "SK": f"SUB#{sub_id}"})


def _compose_payload(notification: dict, alert_id: str | None) -> dict:
    """Self-contained Service-Worker notification payload (no store round-trip)."""
    severity = (notification.get("severity") or "alert").lower()
    source = notification.get("source_id") or notification.get("equipment_id") or ""
    condition = (notification.get("condition") or "").replace("_", " ")
    body = (
        notification.get("message")
        or " ".join(p for p in [source, condition] if p).strip()
        or "New alert"
    )
    return {
        "title": f"{severity.upper()} alert" if severity != "alert" else "Alert",
        "body": body,
        "alert_id": alert_id or notification.get("alert_id"),
        "severity": severity,
        "url": "/voice-spike",
    }


def _send_one(subscription_info: dict, payload: dict, vapid: dict) -> int:
    """Send one Web Push; return the HTTP status. Raises on transport errors other
    than prunable ones (403 VAPID mismatch / 404 / 410 Gone). pywebpush is imported
    lazily for testability."""
    from pywebpush import WebPushException, webpush  # noqa: PLC0415

    try:
        resp = webpush(
            subscription_info=subscription_info,
            data=json.dumps(payload),
            vapid_private_key=vapid["private_key"],
            vapid_claims={"sub": vapid.get("subject", "mailto:ops@example.com")},
            timeout=10,
        )
        return getattr(resp, "status_code", 200)
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in GONE_STATUS:
            return status
        raise


def handler(event: dict, _context) -> dict:
    # Event authenticity: the trusted store identity is the IoT-authenticated
    # topic, NOT the self-declared payload field. A device can publish only to
    # stores/{its-own-thing}/push-requests/*, but could otherwise name a
    # *different* store_id in the body and push an attacker-authored
    # notification to that store's displays (T2). Derive the id from the topic
    # and drop a mismatch.
    topic = event.get("topic")
    store_id = _store_id_from_topic(topic) if isinstance(topic, str) else None
    alert_id = event.get("alert_id")
    notification = event.get("notification") or {}
    print(json.dumps({"marker": "push_request_entry", "store_id": store_id, "alert_id": alert_id}))
    if not store_id:
        return {"status": "dropped", "reason": "missing_or_bad_topic"}
    payload_store_id = event.get("store_id")
    if payload_store_id is not None and payload_store_id != store_id:
        reason = f"store_id_mismatch:topic={store_id},payload={payload_store_id}"
        print(json.dumps({"marker": "push_request_store_mismatch", "reason": reason}))
        return {"status": "dropped", "reason": reason}

    subs = _query_subscriptions(store_id)
    if not subs:
        print(json.dumps({"marker": "push_no_subscriptions", "store_id": store_id}))
        return {"status": "no_subscriptions", "store_id": store_id, "sent": 0}

    vapid = _load_vapid()
    payload = _compose_payload(notification, alert_id)
    sent = 0
    pruned = 0
    for sub in subs:
        info = {
            "endpoint": sub["endpoint"],
            "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
        }
        status = _send_one(info, payload, vapid)
        if status in GONE_STATUS:
            _delete_subscription(store_id, sub["subscription_id"])
            pruned += 1
        else:
            sent += 1
    print(json.dumps({"marker": "push_sent", "store_id": store_id, "sent": sent, "pruned": pruned}))
    return {"status": "sent", "store_id": store_id, "sent": sent, "pruned": pruned}
