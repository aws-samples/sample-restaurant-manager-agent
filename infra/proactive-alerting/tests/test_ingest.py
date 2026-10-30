# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the proactive-alert ingest handler logic (spec 023 Task 4).

Covers envelope validation, the new-alert persist+emit path, DLQ routing for
malformed events, and the atomic conditional-write dedup (idempotency under
re-fire / QoS-1 redelivery). The thin AWS wrappers are monkeypatched.
"""
import importlib.util
import json
import os

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")  # for module-load boto3 clients

import pytest  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

_PATH = os.path.join(os.path.dirname(__file__), "..", "lambdas", "ingest", "main.py")


def _load():
    spec = importlib.util.spec_from_file_location("ingest_main_under_test", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ingest = _load()

_VALID = {
    "schema_version": "1.0", "event_id": "e1", "store_id": "sim-store-001",
    "domain": "equipment", "event_type": "alert_raised", "severity": "critical",
    "source_id": "fryer-1", "condition": "compressor_fault",
    "timestamp": "2026-06-24T14:00:00",
    "dedup_key": "sim-store-001#equipment#fryer-1#compressor_fault",
    "reading": {"current_temp": 395.0, "target_temp": 350.0},
    # topic() AS topic is added by the ingest Topic Rule; it is the
    # IoT-authenticated store identity (see topic_store_id / handler).
    "topic": "stores/sim-store-001/events/equipment",
}


def _wire(monkeypatch, put_raises=None):
    calls = {"put": [], "emit": [], "dlq": [], "bump": []}

    def _put(item):
        calls["put"].append(item)
        if put_raises is not None:
            raise put_raises

    monkeypatch.setattr(ingest, "_put_new_alert", _put)
    monkeypatch.setattr(ingest, "_emit_alert_raised", lambda item: calls["emit"].append(item))
    monkeypatch.setattr(ingest, "_send_to_dlq", lambda e, r: calls["dlq"].append(r))
    monkeypatch.setattr(ingest, "_bump_last_seen", lambda pk, sk, now: calls["bump"].append((pk, sk)))
    return calls


def _conditional_failed() -> ClientError:
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")


def test_validate_envelope_pure():
    assert ingest.validate_envelope(dict(_VALID)) is None
    assert "missing_fields" in ingest.validate_envelope({"event_id": "x"})
    bad = dict(_VALID); bad["severity"] = "meltdown"
    assert "bad_severity" in ingest.validate_envelope(bad)
    assert ingest.validate_envelope("not-a-dict") == "not_a_dict"


def test_valid_event_persists_and_emits(monkeypatch):
    calls = _wire(monkeypatch)
    out = ingest.handler(dict(_VALID), None)
    assert out["status"] == "raised"
    assert len(calls["put"]) == 1 and len(calls["emit"]) == 1
    assert calls["dlq"] == [] and calls["bump"] == []
    item = calls["put"][0]
    assert item["PK"] == "STORE#sim-store-001"
    assert item["SK"].startswith("ALERT#equipment-fryer-1-compressor_fault-")
    assert item["status"] == "active" and item["severity"] == "critical"
    assert item["dedup_key"] == _VALID["dedup_key"]
    # envelope is stored as a JSON string (no float->Decimal hazard)
    assert json.loads(item["envelope_json"])["event_id"] == "e1"


def test_missing_field_routes_to_dlq(monkeypatch):
    calls = _wire(monkeypatch)
    bad = dict(_VALID); del bad["dedup_key"]
    out = ingest.handler(bad, None)
    assert out["status"] == "invalid" and "dedup_key" in out["reason"]
    assert calls["dlq"] and not calls["put"] and not calls["emit"]


def test_bad_severity_routes_to_dlq(monkeypatch):
    calls = _wire(monkeypatch)
    bad = dict(_VALID); bad["severity"] = "meltdown"
    out = ingest.handler(bad, None)
    assert out["status"] == "invalid"
    assert calls["dlq"] and not calls["put"]


def test_topic_store_id_pure():
    assert ingest.topic_store_id(dict(_VALID)) == "sim-store-001"
    assert ingest.topic_store_id({"topic": "stores/other-store/events/inventory"}) == "other-store"
    assert ingest.topic_store_id({}) is None
    assert ingest.topic_store_id({"topic": "garbage"}) is None
    assert ingest.topic_store_id({"topic": "stores//events/equipment"}) is None  # empty store seg
    assert ingest.topic_store_id("not-a-dict") is None


def test_store_id_mismatch_routes_to_dlq(monkeypatch):
    # A device publishing on its own authenticated topic but claiming a
    # different store_id in the body must be rejected (cross-tenant injection).
    calls = _wire(monkeypatch)
    spoof = dict(_VALID)
    spoof["store_id"] = "victim-store"  # topic stays stores/sim-store-001/events/...
    out = ingest.handler(spoof, None)
    assert out["status"] == "invalid" and "store_id_mismatch" in out["reason"]
    assert calls["dlq"] and not calls["put"] and not calls["emit"]


def test_missing_topic_routes_to_dlq(monkeypatch):
    # No authenticated topic -> cannot establish the store identity -> reject.
    calls = _wire(monkeypatch)
    no_topic = dict(_VALID); del no_topic["topic"]
    out = ingest.handler(no_topic, None)
    assert out["status"] == "invalid" and out["reason"] == "missing_or_bad_topic"
    assert calls["dlq"] and not calls["put"] and not calls["emit"]


def test_uses_topic_store_id_downstream(monkeypatch):
    # The persisted PK is built from the topic-derived (authenticated) store id.
    calls = _wire(monkeypatch)
    ev = dict(_VALID)
    ev["topic"] = "stores/sim-store-042/events/equipment"
    ev["store_id"] = "sim-store-042"  # must agree with the topic to pass
    out = ingest.handler(ev, None)
    assert out["status"] == "raised"
    assert calls["put"][0]["PK"] == "STORE#sim-store-042"


def test_dedup_on_conditional_failure_does_not_emit(monkeypatch):
    calls = _wire(monkeypatch, put_raises=_conditional_failed())
    out = ingest.handler(dict(_VALID), None)
    assert out["status"] == "deduplicated"
    # idempotent: a re-fire / QoS-1 redelivery bumps the row, never re-emits.
    assert calls["bump"] and calls["emit"] == []


def test_non_conditional_clienterror_propagates(monkeypatch):
    other = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "PutItem")
    _wire(monkeypatch, put_raises=other)
    with pytest.raises(ClientError):
        ingest.handler(dict(_VALID), None)
