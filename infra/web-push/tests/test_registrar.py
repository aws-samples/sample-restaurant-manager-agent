# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the Web Push registrar handler (spec 025 Task 1d)."""
from __future__ import annotations

import importlib.util
import pathlib


def _load():
    path = pathlib.Path(__file__).resolve().parent.parent / "lambdas/registrar/main.py"
    spec = importlib.util.spec_from_file_location("registrar_main", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_valid_subscription_upserts():
    reg = _load()
    captured = {}
    reg._put_subscription = lambda item: captured.update(item)
    out = reg.handler(
        {
            "topic": "stores/sim-store-001/push-subscriptions",
            "subscription": {
                "endpoint": "https://push.example/abc",
                "keys": {"p256dh": "P", "auth": "A"},
            },
        },
        None,
    )
    assert out["status"] == "registered"
    assert captured["PK"] == "STORE#sim-store-001"
    assert captured["SK"].startswith("SUB#")
    assert captured["endpoint"] == "https://push.example/abc"
    assert captured["p256dh"] == "P" and captured["auth"] == "A"
    assert "ttl" in captured and captured["ttl"] > captured["created_at"]


_SUB = {"endpoint": "https://e", "keys": {"p256dh": "p", "auth": "a"}}


def test_payload_store_id_mismatch_is_rejected():
    # T1: a device publishing on its own authenticated topic but claiming a
    # different store_id in the body must not register a subscription under
    # the victim store (cross-tenant alert disclosure).
    reg = _load()
    called = []
    reg._put_subscription = lambda item: called.append(item)
    out = reg.handler(
        {"topic": "stores/store-a/push-subscriptions", "store_id": "store-b", "subscription": _SUB},
        None,
    )
    assert out["status"] == "invalid" and "store_id_mismatch" in out["reason"]
    assert not called


def test_missing_topic_is_rejected_even_with_payload_store_id():
    # No authenticated topic -> cannot establish the store identity -> reject.
    reg = _load()
    called = []
    reg._put_subscription = lambda item: called.append(item)
    out = reg.handler({"store_id": "s2", "subscription": _SUB}, None)
    assert out == {"status": "invalid", "reason": "missing_or_bad_topic"}
    assert not called


def test_bad_topic_is_rejected():
    reg = _load()
    called = []
    reg._put_subscription = lambda item: called.append(item)
    for topic in ("garbage", "stores/s/events/x", "stores/s/push-subscriptions/extra", 123):
        out = reg.handler({"topic": topic, "subscription": _SUB}, None)
        assert out["reason"] == "missing_or_bad_topic", topic
    assert not called


def test_matching_payload_store_id_uses_topic_store():
    # The real gateway sends store_id in the body equal to its topic segment.
    reg = _load()
    captured = {}
    reg._put_subscription = lambda item: captured.update(item)
    out = reg.handler(
        {"topic": "stores/sim-store-042/push-subscriptions", "store_id": "sim-store-042",
         "subscription": _SUB},
        None,
    )
    assert out["status"] == "registered"
    assert captured["PK"] == "STORE#sim-store-042" and captured["store_id"] == "sim-store-042"


def test_missing_keys_is_invalid_and_does_not_write():
    reg = _load()
    called = []
    reg._put_subscription = lambda item: called.append(item)
    out = reg.handler(
        {"topic": "stores/s/push-subscriptions", "subscription": {"endpoint": "https://e"}},
        None,
    )
    assert out["status"] == "invalid"
    assert not called


def test_subscription_id_is_stable_per_endpoint():
    reg = _load()
    assert reg.subscription_id("https://e/abc") == reg.subscription_id("https://e/abc")
    assert reg.subscription_id("https://e/abc") != reg.subscription_id("https://e/xyz")
