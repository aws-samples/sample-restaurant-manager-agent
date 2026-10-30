# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the ack handler (spec 023 Task 7)."""
import importlib.util
import os

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")  # module-load boto3 clients

import pytest  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

_PATH = os.path.join(os.path.dirname(__file__), "..", "lambdas", "ack", "main.py")


def _load():
    spec = importlib.util.spec_from_file_location("ack_main_under_test", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ack = _load()
# Fake values live in neutrally named constants: Bandit B105/B106 flag a string
# *literal* bound to a secret-sounding name, even in tests.
_FAKE_TT = "tok1"
_EVENT = {"topic": "stores/sim-store-001/events/ack", "alert_id": "equipment-fryer-1-compressor_fault-1"}


def _wire(monkeypatch, item, send_raises=None):
    calls = {"success": [], "marked": []}

    def _send(token, result):
        calls["success"].append((token, result))
        if send_raises is not None:
            raise send_raises

    monkeypatch.setattr(ack, "_get_alert", lambda s, a: item)
    monkeypatch.setattr(ack, "_send_task_success", _send)
    monkeypatch.setattr(ack, "_mark_acknowledged", lambda s, a: calls["marked"].append((s, a)))
    return calls


def test_resume_and_mark(monkeypatch):
    calls = _wire(monkeypatch, {"task_token": _FAKE_TT})
    out = ack.handler(dict(_EVENT), None)
    assert out["status"] == "acknowledged"
    assert calls["success"][0][0] == _FAKE_TT
    assert calls["marked"] == [("sim-store-001", "equipment-fryer-1-compressor_fault-1")]


def test_no_token_is_noop(monkeypatch):
    calls = _wire(monkeypatch, {"status": "acknowledged"})  # no task_token
    out = ack.handler(dict(_EVENT), None)
    assert out["status"] == "no_token"
    assert calls["success"] == [] and calls["marked"] == []


def test_unknown_alert_is_noop(monkeypatch):
    calls = _wire(monkeypatch, None)  # GetItem returns no item
    out = ack.handler(dict(_EVENT), None)
    assert out["status"] == "no_token" and calls["success"] == []


def test_benign_send_error_still_marks(monkeypatch):
    err = ClientError({"Error": {"Code": "TaskTimedOut"}}, "SendTaskSuccess")
    calls = _wire(monkeypatch, {"task_token": _FAKE_TT}, send_raises=err)
    out = ack.handler(dict(_EVENT), None)
    assert out["status"] == "acknowledged"  # the wait was already gone; benign
    assert calls["marked"] == [("sim-store-001", "equipment-fryer-1-compressor_fault-1")]


def test_fatal_send_error_propagates(monkeypatch):
    err = ClientError({"Error": {"Code": "ThrottlingException"}}, "SendTaskSuccess")
    _wire(monkeypatch, {"task_token": _FAKE_TT}, send_raises=err)
    with pytest.raises(ClientError):
        ack.handler(dict(_EVENT), None)


def test_missing_ids_dropped(monkeypatch):
    _wire(monkeypatch, {"task_token": _FAKE_TT})
    out = ack.handler({"topic": "garbage"}, None)
    assert out["status"] == "dropped"


def test_store_id_mismatch_dropped(monkeypatch):
    # Body claims a different store than the authenticated topic -> rejected
    # before any token resume (cross-tenant ack / alert suppression).
    calls = _wire(monkeypatch, {"task_token": _FAKE_TT})
    ev = dict(_EVENT); ev["store_id"] = "victim-store"  # topic is sim-store-001
    out = ack.handler(ev, None)
    assert out["status"] == "dropped" and out["reason"] == "store_id_mismatch"
    assert calls["success"] == [] and calls["marked"] == []
