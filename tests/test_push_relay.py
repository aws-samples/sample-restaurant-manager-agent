# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Web Push relay + registration tests (spec 025 Task 1d).

Covers the store backend half: the VAPID-public-key + subscribe routes, the
store relay/re-engagement seams (no-op until the bridge is wired), and the
general ``publish_store_topic`` topic format. The cloud sender + live delivery
are covered in infra/web-push and validated on deploy (Task 1f).
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from simulator.api.routes_push import router as push_router
from simulator.engine.clock import VirtualClock
from simulator.models.simulation import SimulationConfig
from simulator.mqtt.publisher import ResponsePublisher
from simulator.state.store import StateStore, load_all_configs


def _store() -> StateStore:
    configs = load_all_configs("config")
    config = SimulationConfig(**configs["simulation"])
    clock = VirtualClock(datetime(2026, 1, 1, 7, 0, 0), speed_multiplier=10, tick_interval_seconds=1)
    return StateStore(clock_state=clock.get_state(), config=config)


@pytest.fixture
def client_and_store():
    store = _store()
    app = FastAPI()
    app.include_router(push_router, prefix="/api")
    app.state.store = store
    return TestClient(app), store


def test_vapid_public_key_from_env(client_and_store, monkeypatch):
    client, _ = client_and_store
    from simulator.api import routes_push
    monkeypatch.setattr(routes_push, "_cache", {"value": None, "at": 0.0})
    # No SSM in the test env -> fetch returns None -> env fallback.
    monkeypatch.setattr(routes_push, "_fetch_public_key_from_ssm", lambda: None)
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "BPUBLIC123")
    r = client.get("/api/push/vapid-public-key")
    assert r.status_code == 200 and r.json()["publicKey"] == "BPUBLIC123"


def test_vapid_public_key_prefers_ssm_over_env(client_and_store, monkeypatch):
    # spec 028: the current key comes from SSM (kept current by the rotator),
    # winning over the (stale) deploy-time env fallback.
    client, _ = client_and_store
    from simulator.api import routes_push
    monkeypatch.setattr(routes_push, "_cache", {"value": None, "at": 0.0})
    monkeypatch.setattr(routes_push, "_fetch_public_key_from_ssm", lambda: "BSSM_ROTATED")
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "BSTALE_ENV")
    r = client.get("/api/push/vapid-public-key")
    assert r.json()["publicKey"] == "BSSM_ROTATED"


def test_vapid_public_key_env_fallback_when_ssm_unset(client_and_store, monkeypatch):
    # An unprovisioned param returns the "UNSET" placeholder -> env fallback.
    client, _ = client_and_store
    from simulator.api import routes_push
    monkeypatch.setattr(routes_push, "_cache", {"value": None, "at": 0.0})
    monkeypatch.setattr(routes_push, "_fetch_public_key_from_ssm", lambda: "UNSET")
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "BENV_FALLBACK")
    r = client.get("/api/push/vapid-public-key")
    assert r.json()["publicKey"] == "BENV_FALLBACK"


def test_subscribe_relays_when_wired(client_and_store):
    client, store = client_and_store
    relayed = []

    async def _pub(sub):
        relayed.append(sub)

    store.push_subscription_publisher = _pub
    sub = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "P", "auth": "A"}}
    r = client.post("/api/push/subscribe", json={"subscription": sub})
    assert r.status_code == 200 and r.json() == {"ok": True, "relayed": True}
    assert relayed == [sub]


def test_subscribe_noop_when_bridge_unwired(client_and_store):
    client, _store = client_and_store  # publisher is None by default
    r = client.post(
        "/api/push/subscribe",
        json={"subscription": {"endpoint": "https://e", "keys": {"p256dh": "p", "auth": "a"}}},
    )
    assert r.status_code == 200 and r.json()["relayed"] is False


def test_relay_push_subscription_empty_is_noop():
    store = _store()
    called = []

    async def _pub(sub):
        called.append(sub)

    store.push_subscription_publisher = _pub
    assert asyncio.run(store.relay_push_subscription({})) is False
    assert not called
    assert asyncio.run(store.relay_push_subscription({"endpoint": "x"})) is True
    assert called == [{"endpoint": "x"}]


def test_request_push_reengagement_gated_on_publisher():
    store = _store()
    assert asyncio.run(store.request_push_reengagement("a1", {"severity": "critical"})) is False
    sent = []

    async def _req(alert_id, notif):
        sent.append((alert_id, notif))

    store.push_request_publisher = _req
    assert asyncio.run(store.request_push_reengagement("a1", {"severity": "critical"})) is True
    assert sent == [("a1", {"severity": "critical"})]


def test_publish_store_topic_format():
    published = []

    class _FakeClient:
        is_connected = True

        async def publish(self, topic, payload, qos=1):
            published.append((topic, payload, qos))

    pub = ResponsePublisher(_FakeClient(), "sim-store-001", 16)
    asyncio.run(pub.publish_store_topic("push-subscriptions", b"{}"))
    asyncio.run(pub.publish_store_topic("push-requests/a1", b"{}"))
    topics = [p[0] for p in published]
    assert topics == [
        "stores/sim-store-001/push-subscriptions",
        "stores/sim-store-001/push-requests/a1",
    ]


def test_handle_incoming_notification_requests_push_when_no_display():
    store = _store()
    sent = []

    async def _req(alert_id, notif):
        sent.append((alert_id, notif))

    store.push_request_publisher = _req
    out = asyncio.run(
        store.handle_incoming_notification(
            {"alert_id": "a1", "severity": "critical"},
            sim_time=datetime(2026, 1, 1, 7, 0, 0),
            display_connected=False,
        )
    )
    assert out["inboxed"] is True and out["push_requested"] is True
    assert sent == [("a1", {"alert_id": "a1", "severity": "critical"})]


def test_handle_incoming_notification_no_push_when_display_connected():
    store = _store()
    sent = []

    async def _req(alert_id, notif):
        sent.append(alert_id)

    store.push_request_publisher = _req
    out = asyncio.run(
        store.handle_incoming_notification(
            {"alert_id": "a2"},
            sim_time=datetime(2026, 1, 1, 7, 0, 0),
            display_connected=True,
        )
    )
    assert out["inboxed"] is True and out["push_requested"] is False
    assert not sent
