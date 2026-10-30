# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for the IoT response handler (spec 020 Task 1/3).

Two layers here, deliberately:

* handler tests, which stub the DynamoDB helpers to exercise the control flow; and
* helper tests for ``_to_dynamo_numbers`` / ``update_command_result_terminal``,
  which do NOT stub the serialization. That second layer is new, and it exists
  because stubbing ``update_state_cache`` wholesale is exactly what hid the bug
  this file now guards: the real write rejected float payloads, and no test ever
  touched the real write.
"""
from __future__ import annotations

import os
import pathlib
from decimal import Decimal

os.environ.setdefault("COMMAND_RESULTS_TABLE_NAME", "CommandResults")
os.environ.setdefault("STORE_STATE_CACHE_TABLE_NAME", "StoreStateCache")
import ddb_ops  # response_handler/ddb_ops.py
import main as rh  # response_handler/main.py


def _patch(monkeypatch, row, written=True):
    """Stub the three DynamoDB helpers; return the recorded calls."""
    calls: dict[str, list] = {"cache": [], "terminal": []}
    monkeypatch.setattr(rh, "get_command_result_item", lambda cid: row)
    monkeypatch.setattr(rh, "update_state_cache", lambda **kw: calls["cache"].append(kw))
    monkeypatch.setattr(
        rh, "update_command_result_terminal",
        lambda **kw: (calls["terminal"].append(kw), written)[1],
    )
    return calls


# --- the terminal write is the point of the handler ---------------------------

def test_terminal_write_happens_without_any_callback(monkeypatch):
    """The regression this file exists for.

    A row with **no** ``callback_id`` is the normal case in this deployment (the
    durable layer that wrote them was destroyed in spec 022 Task 6). The handler
    used to return early here, so the row aged out on its TTL and the cache never
    filled -- silently, with every transport hop healthy. Both writes must happen.
    """
    row = {"correlation_id": "c1", "tool_name": "get_equipment_status",
           "domain": "equipment", "store_id": "sim-store-001"}  # no callback_id
    calls = _patch(monkeypatch, row)
    out = rh.handler({"correlation_id": "c1", "status": "success", "duration_ms": 12,
                      "result": {"equipment": [{"id": "fryer_1", "current_temp": 351.5}]},
                      "topic": "stores/sim-store-001/responses/c1"}, None)

    assert out["status"] == "resolved"
    assert out["cached"] is True
    assert len(calls["terminal"]) == 1
    assert calls["terminal"][0]["status"] == "success"
    assert calls["terminal"][0]["duration_ms"] == 12
    assert len(calls["cache"]) == 1
    assert calls["cache"][0]["domain"] == "equipment"


def test_terminal_write_records_error_status(monkeypatch):
    row = {"correlation_id": "c2", "tool_name": "get_stock_levels",
           "domain": "inventory", "store_id": "sim-store-001"}
    calls = _patch(monkeypatch, row)
    out = rh.handler({"correlation_id": "c2", "status": "error",
                      "result": {"message": "sensor offline"},
                      "topic": "stores/sim-store-001/responses/c2"}, None)

    assert calls["terminal"][0]["status"] == "error"
    # A failed call must not poison the last-known-state cache.
    assert calls["cache"] == []
    assert out["cached"] is False


def test_callback_resume_is_gone(monkeypatch):
    """The durable-callback resume must stay removed (spec 020 R3.1, retired).

    A row carrying a ``callback_id`` -- which nothing writes any more -- must be
    resolved exactly like any other row, with no attempt to resume anything. This
    is a coupling guard, not a behaviour test: re-introducing the branch is how the
    R5.2/R5.3 writes came to sit behind a dead consumer in the first place.
    """
    row = {"correlation_id": "c3", "callback_id": "cb3", "tool_name": "get_daily_sales",
           "domain": "pos", "store_id": "sim-store-001"}
    calls = _patch(monkeypatch, row)
    out = rh.handler({"correlation_id": "c3", "status": "success", "result": {},
                      "topic": "stores/sim-store-001/responses/c3"}, None)

    assert out["status"] == "resolved"
    assert "callback" not in out, "the response must not report a callback outcome"
    assert len(calls["terminal"]) == 1  # resolved on its own merits


def test_no_durable_callback_surface_remains():
    """Neither module may reference the durable-execution callback API.

    Also guards the 31 MB vendored boto3 that only existed to reach it (spec 020
    R3.4, retired): the handler now needs nothing beyond the runtime's own boto3, so
    re-adding a call here would silently reintroduce a bundling requirement.
    """
    for module in (rh, ddb_ops):
        source = pathlib.Path(module.__file__).read_text()
        assert "send_durable_execution_callback" not in source
        assert "send_callback_success" not in source
    assert not hasattr(ddb_ops, "send_callback_success")
    # The lambda client existed solely for the callback.
    assert not hasattr(ddb_ops, "_lambda_client")


def test_blocked_conditional_write_reported_not_raised(monkeypatch):
    row = {"correlation_id": "c5", "tool_name": "get_daily_sales",
           "domain": "pos", "store_id": "sim-store-001"}
    _patch(monkeypatch, row, written=False)
    out = rh.handler({"correlation_id": "c5", "status": "success", "result": {},
                      "topic": "stores/sim-store-001/responses/c5"}, None)
    assert out["status"] == "not_written"


def test_cache_write_failure_does_not_fail_resolution(monkeypatch):
    """Best-effort cache, but the terminal write must still stand."""
    row = {"correlation_id": "c6", "tool_name": "get_labor_cost",
           "domain": "schedule", "store_id": "sim-store-001"}
    calls = _patch(monkeypatch, row)

    def _boom(**_kw):
        raise TypeError("Float types are not supported")

    monkeypatch.setattr(rh, "update_state_cache", _boom)
    out = rh.handler({"correlation_id": "c6", "status": "success", "result": {"cost": 1.5},
                      "topic": "stores/sim-store-001/responses/c6"}, None)
    assert out["status"] == "resolved"
    assert out["cached"] is False
    assert len(calls["terminal"]) == 1


# --- routing / dispatch behaviour (unchanged contract) ------------------------

def test_domain_inferred_from_tool_name_when_row_omits_it(monkeypatch):
    row = {"correlation_id": "c7", "tool_name": "get_stock_levels",
           "store_id": "sim-store-001"}  # no domain
    calls = _patch(monkeypatch, row)
    rh.handler({"correlation_id": "c7", "status": "success", "result": {},
                "topic": "stores/sim-store-001/responses/c7"}, None)
    assert calls["cache"][0]["domain"] == "inventory"


def test_mutating_tool_not_cached(monkeypatch):
    row = {"correlation_id": "c8", "tool_name": "void_order",
           "domain": "pos", "store_id": "sim-store-001"}
    calls = _patch(monkeypatch, row)
    out = rh.handler({"correlation_id": "c8", "status": "success", "result": {"voided": True},
                      "topic": "stores/sim-store-001/responses/c8"}, None)
    assert out["status"] == "resolved"
    assert calls["cache"] == []  # void_order is not a cacheable read tool


def test_correlation_falls_back_to_topic(monkeypatch):
    row = {"correlation_id": "c9", "tool_name": "get_labor_cost", "store_id": "sim-store-001"}
    calls = _patch(monkeypatch, row)
    out = rh.handler({"status": "success", "result": {},
                      "topic": "stores/sim-store-001/responses/c9"}, None)
    assert out["status"] == "resolved"
    assert calls["terminal"][0]["correlation_id"] == "c9"


def test_drop_when_row_is_unknown(monkeypatch):
    calls = _patch(monkeypatch, None)
    out = rh.handler({"correlation_id": "missing", "topic": "stores/s/responses/missing"}, None)
    assert out["status"] == "dropped"
    assert out["reason"] == "unknown_correlation"
    # Nothing to resolve means nothing written.
    assert calls["terminal"] == [] and calls["cache"] == []


def test_drop_dead_letter(monkeypatch):
    calls = _patch(monkeypatch, {"callback_id": "x"})
    out = rh.handler({"topic": "stores/sim-store-001/responses/_invalid"}, None)
    assert out["status"] == "dropped"
    assert calls["terminal"] == []


# --- serialization, unstubbed ------------------------------------------------

def test_floats_are_converted_for_dynamodb():
    """``float`` must become ``Decimal`` or the resource API rejects the write.

    boto3's DynamoDB resource raises ``TypeError: Float types are not supported``
    on any float, and store results are full of them (``current_temp``,
    ``target_temp``, labor cost). The old code passed the payload through raw, so
    every cache write for a float-bearing read tool failed into a best-effort
    ``except`` and the cache stayed permanently empty.
    """
    out = ddb_ops._to_dynamo_numbers(
        {"equipment": [{"id": "fryer_1", "current_temp": 351.5, "on": True, "count": 3}]}
    )
    item = out["equipment"][0]
    assert item["current_temp"] == Decimal("351.5")
    assert isinstance(item["current_temp"], Decimal)
    # bool is an int subclass -- it must stay a bool, not become a number.
    assert item["on"] is True
    # ints are already valid DynamoDB numbers; leave them alone.
    assert item["count"] == 3 and isinstance(item["count"], int)


def test_float_conversion_avoids_binary_noise():
    """``Decimal(str(v))``, not ``Decimal(v)``.

    ``Decimal(0.1)`` is 0.1000000000000000055511151231257827, which both bloats the
    item and makes stored values fail an equality check against the source.
    """
    assert ddb_ops._to_dynamo_numbers(0.1) == Decimal("0.1")


def test_converted_payload_is_dynamodb_serializable():
    """End-to-end proof of the property, using boto3's own serializer.

    This is the check that would have caught the original bug: it asserts the raw
    payload is rejected and the converted one is accepted, so the conversion is
    load-bearing rather than decorative.
    """
    from boto3.dynamodb.types import TypeSerializer

    serializer = TypeSerializer()
    payload = {"equipment": [{"current_temp": 351.5}]}

    try:
        serializer.serialize(payload)
        raise AssertionError("expected boto3 to reject a raw float payload")
    except TypeError as exc:
        assert "Float types are not supported" in str(exc)

    serializer.serialize(ddb_ops._to_dynamo_numbers(payload))  # must not raise


def test_decimal_roundtrip_is_symmetric():
    """The read-side converter must undo the write-side one."""
    original = {"temp": 351.5, "count": 3, "nested": [{"x": 0.25}]}
    stored = ddb_ops._to_dynamo_numbers(original)
    assert ddb_ops._convert_decimals(stored) == original
