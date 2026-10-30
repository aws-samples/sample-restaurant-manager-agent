# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the Web Push sender handler (spec 025 Task 1d).

pywebpush is never imported: the tests monkeypatch the thin ``_send_one`` /
DynamoDB / Secrets wrappers, so the orchestration is verified without the native
web-push dependency installed.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest


def _load():
    path = pathlib.Path(__file__).resolve().parent.parent / "lambdas/push_sender/main.py"
    spec = importlib.util.spec_from_file_location("push_sender_main", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_compose_payload_shape():
    ps = _load()
    p = ps._compose_payload(
        {
            "severity": "critical",
            "source_id": "freezer_1",
            "condition": "thermostat_malfunction",
            "message": "Freezer 1 thermostat malfunction (28F)",
        },
        "a1",
    )
    assert p["title"] == "CRITICAL alert"
    assert "Freezer 1" in p["body"]
    assert p["alert_id"] == "a1"
    assert p["severity"] == "critical"
    assert p["url"] == "/voice-spike"


def test_compose_payload_fallback_body_from_source_condition():
    ps = _load()
    p = ps._compose_payload({"severity": "warning", "source_id": "fryer_2", "condition": "over_temp"}, None)
    assert p["body"] == "fryer_2 over temp"


def test_handler_sends_to_all_subscriptions():
    ps = _load()
    ps._query_subscriptions = lambda sid: [
        {"subscription_id": "s1", "endpoint": "https://e1", "p256dh": "p1", "auth": "a1"},
        {"subscription_id": "s2", "endpoint": "https://e2", "p256dh": "p2", "auth": "a2"},
    ]
    ps._load_vapid = lambda: {"private_key": "k", "subject": "mailto:x"}
    sent_infos = []
    ps._send_one = lambda info, payload, vapid: (sent_infos.append(info), 201)[1]
    deleted = []
    ps._delete_subscription = lambda sid, subid: deleted.append(subid)

    out = ps.handler(
        {
            "topic": "stores/sim-store-001/push-requests/a1",
            "alert_id": "a1",
            "notification": {"severity": "critical", "message": "m"},
        },
        None,
    )
    assert out["sent"] == 2 and out["pruned"] == 0
    assert [i["endpoint"] for i in sent_infos] == ["https://e1", "https://e2"]
    assert not deleted


def test_handler_prunes_gone_subscription():
    ps = _load()
    ps._query_subscriptions = lambda sid: [
        {"subscription_id": "good", "endpoint": "https://g", "p256dh": "p", "auth": "a"},
        {"subscription_id": "gone", "endpoint": "https://x", "p256dh": "p", "auth": "a"},
    ]
    ps._load_vapid = lambda: {"private_key": "k", "subject": "mailto:x"}
    ps._send_one = lambda info, payload, vapid: 410 if info["endpoint"] == "https://x" else 201
    deleted = []
    ps._delete_subscription = lambda sid, subid: deleted.append(subid)

    out = ps.handler({"topic": "stores/s/push-requests/a1", "store_id": "s", "notification": {}}, None)
    assert out["sent"] == 1 and out["pruned"] == 1
    assert deleted == ["gone"]


def test_handler_no_subscriptions():
    ps = _load()
    ps._query_subscriptions = lambda sid: []
    out = ps.handler({"topic": "stores/s/push-requests/a1", "store_id": "s", "notification": {}}, None)
    assert out["status"] == "no_subscriptions" and out["sent"] == 0


def test_handler_missing_store_id_drops():
    ps = _load()
    out = ps.handler({"topic": "bad/topic", "notification": {}}, None)
    assert out == {"status": "dropped", "reason": "missing_or_bad_topic"}


def test_missing_topic_drops_even_with_payload_store_id():
    # No authenticated topic -> cannot establish the store identity -> drop.
    ps = _load()
    ps._query_subscriptions = lambda sid: pytest.fail("must not look up subscriptions")
    out = ps.handler({"store_id": "s", "notification": {}}, None)
    assert out == {"status": "dropped", "reason": "missing_or_bad_topic"}


def test_payload_store_id_mismatch_sends_nothing():
    # T2: a device on its own authenticated topic naming another store in the
    # body must not reach the victim store's subscriptions (forged alerts).
    ps = _load()
    ps._query_subscriptions = lambda sid: pytest.fail("must not look up subscriptions")
    ps._load_vapid = lambda: pytest.fail("must not load VAPID key")
    ps._send_one = lambda *a: pytest.fail("must not send")
    out = ps.handler(
        {
            "topic": "stores/store-a/push-requests/x",
            "store_id": "store-b",
            "notification": {"severity": "critical", "message": "FAKE: discard all product"},
        },
        None,
    )
    assert out["status"] == "dropped" and "store_id_mismatch" in out["reason"]


def test_uses_topic_store_id_for_lookup():
    ps = _load()
    queried = []
    ps._query_subscriptions = lambda sid: (queried.append(sid), [])[1]
    out = ps.handler(
        {"topic": "stores/sim-store-042/push-requests/a1", "store_id": "sim-store-042",
         "notification": {}},
        None,
    )
    assert out["status"] == "no_subscriptions"
    assert queried == ["sim-store-042"]


def test_store_id_from_request_topic():
    ps = _load()
    assert ps._store_id_from_topic("stores/sim-store-001/push-requests/alert-9") == "sim-store-001"
    assert ps._store_id_from_topic("bad/topic") is None


def test_handler_prunes_vapid_mismatch_403():
    # spec 028: after a key rotation, subs made against the old public key fail
    # with 403 -> prune them (not raise), same as 404/410.
    ps = _load()
    ps._query_subscriptions = lambda sid: [
        {"subscription_id": "rotated", "endpoint": "https://x", "p256dh": "p", "auth": "a"},
    ]
    ps._load_vapid = lambda: {"private_key": "k", "subject": "mailto:x"}
    ps._send_one = lambda info, payload, vapid: 403
    deleted = []
    ps._delete_subscription = lambda sid, subid: deleted.append(subid)

    out = ps.handler({"topic": "stores/s/push-requests/a1", "store_id": "s", "notification": {}}, None)
    assert out["sent"] == 0 and out["pruned"] == 1
    assert deleted == ["rotated"]


def test_load_vapid_not_cached_reflects_rotation():
    # spec 028: no process-level cache, so a rotated key is picked up immediately.
    ps = _load()

    class _Secrets:
        def __init__(self):
            self._vals = [
                {"SecretString": '{"private_key": "k1", "subject": "mailto:x"}'},
                {"SecretString": '{"private_key": "k2", "subject": "mailto:x"}'},
            ]
            self.i = 0

        def get_secret_value(self, SecretId):
            v = self._vals[min(self.i, len(self._vals) - 1)]
            self.i += 1
            return v

    ps._secrets = _Secrets()
    assert ps._load_vapid()["private_key"] == "k1"
    assert ps._load_vapid()["private_key"] == "k2"
