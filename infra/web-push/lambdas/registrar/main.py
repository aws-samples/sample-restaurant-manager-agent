# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Web Push subscription registrar (spec 025 Task 1d).

Triggered by the IoT Topic Rule on ``stores/+/push-subscriptions``. The store
backend republishes a browser's Web Push subscription here (the store has no
inbound path), and this handler upserts it into the PushSubscriptions table keyed
by a stable hash of the push endpoint, so a re-subscribe overwrites in place.

The store id is taken ONLY from the IoT-authenticated topic, never the
self-declared payload; a payload/topic mismatch (or a missing topic) is rejected
-- the same rule as the alert ingest handler (threat model T1 / M15).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SUBSCRIPTIONS_TABLE_NAME = os.environ.get("SUBSCRIPTIONS_TABLE_NAME", "PushSubscriptions")
SUBSCRIPTION_TTL_SECONDS = int(os.environ.get("SUBSCRIPTION_TTL_DAYS", "90")) * 86400

_table = boto3.resource("dynamodb").Table(SUBSCRIPTIONS_TABLE_NAME)


def _store_id_from_topic(topic: str) -> str | None:
    parts = topic.split("/")
    # stores/{store_id}/push-subscriptions
    if len(parts) == 3 and parts[0] == "stores" and parts[2] == "push-subscriptions":
        return parts[1]
    return None


def subscription_id(endpoint: str) -> str:
    """Stable id derived from the push endpoint (so a re-subscribe upserts)."""
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:24]


def _put_subscription(item: dict) -> None:
    _table.put_item(Item=item)


def handler(event: dict, _context) -> dict:
    # Event authenticity: the trusted store identity is the IoT-authenticated
    # topic, NOT the self-declared payload field. The fleet thing-policy lets a
    # device publish only to stores/{its-own-thing}/push-subscriptions, but
    # nothing stops it putting a *different* store_id in the body -- which would
    # register its browser for another store's alerts (T1). Derive the id from
    # the topic and reject a mismatch.
    topic = event.get("topic")
    store_id = _store_id_from_topic(topic) if isinstance(topic, str) else None
    if not store_id:
        print(json.dumps({"marker": "push_register_invalid", "reason": "missing_or_bad_topic"}))
        return {"status": "invalid", "reason": "missing_or_bad_topic"}
    payload_store_id = event.get("store_id")
    if payload_store_id is not None and payload_store_id != store_id:
        reason = f"store_id_mismatch:topic={store_id},payload={payload_store_id}"
        print(json.dumps({"marker": "push_register_store_mismatch", "reason": reason}))
        return {"status": "invalid", "reason": reason}

    subscription = event.get("subscription") or {}
    endpoint = subscription.get("endpoint")
    keys = subscription.get("keys") or {}
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")

    if not (store_id and endpoint and p256dh and auth):
        print(json.dumps({"marker": "push_register_invalid", "store_id": store_id,
                          "has_endpoint": bool(endpoint)}))
        return {"status": "invalid", "reason": "missing_fields"}

    sub_id = subscription_id(endpoint)
    now = int(time.time())
    _put_subscription({
        "PK": f"STORE#{store_id}",
        "SK": f"SUB#{sub_id}",
        "subscription_id": sub_id,
        "store_id": store_id,
        "endpoint": endpoint,
        "p256dh": p256dh,
        "auth": auth,
        "created_at": now,
        "ttl": now + SUBSCRIPTION_TTL_SECONDS,
    })
    print(json.dumps({"marker": "push_registered", "store_id": store_id, "sub_id": sub_id}))
    return {"status": "registered", "store_id": store_id, "subscription_id": sub_id}
