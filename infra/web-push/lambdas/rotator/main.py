# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""VAPID secret rotation Lambda (spec 028).

Invoked on a schedule (EventBridge rule, <=90 days) and on demand. Each run:

1. Reads the existing VAPID secret only to **preserve the ``subject``** claim.
2. Generates a fresh EC P-256 keypair (``keygen.generate_vapid``).
3. ``PutSecretValue`` the new ``{private_key, public_key, subject}`` (AWSCURRENT).
4. ``PutParameter`` the new **public** key to the SSM parameter the store backend
   reads (``VAPID_PUBLIC_KEY_PARAM``) -- the PRIVATE key never leaves the cloud.

Hard cutover (spec 028 Option B): subscriptions bound to the previous public key
now fail with HTTP 403 and are pruned by the push-sender; the display silently
re-subscribes with the new public key. The marker never logs the private key.
"""
from __future__ import annotations

import json
import logging
import os
import time

import boto3

from keygen import generate_vapid

logger = logging.getLogger()
logger.setLevel(logging.INFO)

VAPID_SECRET_ARN = os.environ.get("VAPID_SECRET_ARN", "")
VAPID_PUBLIC_KEY_PARAM = os.environ.get("VAPID_PUBLIC_KEY_PARAM", "/restaurant-sim/web-push/vapid-public-key")
DEFAULT_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:ops@example.com")

_secrets = boto3.client("secretsmanager")
_ssm = boto3.client("ssm")


def _current_subject() -> str:
    """Preserve the existing subject claim; fall back to the default."""
    try:
        resp = _secrets.get_secret_value(SecretId=VAPID_SECRET_ARN)
        return (json.loads(resp["SecretString"]) or {}).get("subject") or DEFAULT_SUBJECT
    except Exception:  # noqa: BLE001 - first rotation / unreadable value -> default
        logger.warning("could not read existing subject; using default", exc_info=True)
        return DEFAULT_SUBJECT


def handler(event, _context) -> dict:
    subject = _current_subject()
    keys = generate_vapid(subject)  # {private_key, public_key, subject}

    _secrets.put_secret_value(SecretId=VAPID_SECRET_ARN, SecretString=json.dumps(keys))
    _ssm.put_parameter(
        Name=VAPID_PUBLIC_KEY_PARAM, Value=keys["public_key"], Type="String", Overwrite=True
    )

    # Marker for observability -- PUBLIC key + timestamp only, NEVER the private key.
    print(json.dumps({
        "marker": "vapid_rotated",
        "public_key": keys["public_key"],
        "param": VAPID_PUBLIC_KEY_PARAM,
        "rotated_at": int(time.time()),
    }))
    return {"status": "rotated", "public_key": keys["public_key"]}
