# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Web Push registration routes (spec 025 Task 1d; current-key serving spec 028).

The browser (display) registers its Web Push subscription here after explicit
consent; the store relays it to the cloud registry over its outbound IoT
connection (the store has no inbound path). The VAPID PUBLIC key is served to the
browser so it can subscribe; the PRIVATE key never leaves the cloud.

spec 028: the public key is read from an SSM parameter (updated by the rotation
Lambda) so it stays current across a key rotation without a redeploy. The store
role has ``ssm:GetParameter`` on that parameter ONLY -- never the private secret.
Falls back to the ``VAPID_PUBLIC_KEY`` env for local dev / SSM-unavailable.
"""
import os
import time

from fastapi import APIRouter, Request

router = APIRouter(tags=["push"])

VAPID_PUBLIC_KEY_PARAM = os.environ.get(
    "VAPID_PUBLIC_KEY_PARAM", "/restaurant-sim/web-push/vapid-public-key"
)
_PARAM_TTL_S = 60
_cache: dict = {"value": None, "at": 0.0}
_ssm = None  # lazy boto3 client so local dev without AWS still imports


def _fetch_public_key_from_ssm() -> str | None:
    """Return the current public key from SSM, or None if unavailable/unset."""
    global _ssm
    if not VAPID_PUBLIC_KEY_PARAM:
        return None
    try:
        if _ssm is None:
            import boto3  # noqa: PLC0415 - lazy so import works without AWS
            from botocore.config import Config  # noqa: PLC0415
            # Region must be explicit: the backend process has no AWS_REGION in
            # its env, and boto3 does not reliably resolve region from IMDS, so an
            # omitted region raises NoRegionError (caught below -> empty key).
            # Matches the codebase pattern (AWS_REGION default us-west-2).
            region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"
            # Request-path read: fail fast (this endpoint must never hang) — the
            # 60s cache means a healthy path calls SSM at most once a minute.
            _ssm = boto3.client(
                "ssm",
                region_name=region,
                config=Config(connect_timeout=2, read_timeout=2, retries={"max_attempts": 1}),
            )
        value = (_ssm.get_parameter(Name=VAPID_PUBLIC_KEY_PARAM).get("Parameter") or {}).get("Value")
        return value or None
    except Exception:  # noqa: BLE001 - never fail the endpoint on SSM issues
        return None


def _current_public_key() -> str:
    """Current VAPID public key: SSM (cached ~60s) with env fallback."""
    now = time.time()
    if _cache["value"] is not None and (now - _cache["at"]) < _PARAM_TTL_S:
        return _cache["value"]
    value = _fetch_public_key_from_ssm()
    if value and value != "UNSET":
        _cache["value"] = value
        _cache["at"] = now
        return value
    # Not provisioned / SSM unavailable -> env fallback (don't cache, retry next call).
    return os.environ.get("VAPID_PUBLIC_KEY", "")


@router.get("/push/vapid-public-key")
async def vapid_public_key():
    """Return the CURRENT VAPID application-server PUBLIC key (spec 028).

    Read from the SSM parameter the rotation Lambda keeps current; falls back to
    the ``VAPID_PUBLIC_KEY`` env. Empty string until provisioned."""
    return {"publicKey": _current_public_key()}


@router.post("/push/subscribe")
async def subscribe(request: Request):
    """Accept a browser push subscription and relay it to the cloud registry.

    ``relayed`` is False when the store's outbound bridge isn't wired (e.g. local
    dev without MQTT) -- the endpoint still succeeds so the browser-side consent
    flow can be exercised; live relay is validated on deploy (Task 1f)."""
    body = await request.json()
    subscription = body.get("subscription") or {}
    relayed = await request.app.state.store.relay_push_subscription(subscription)
    return {"ok": True, "relayed": relayed}
