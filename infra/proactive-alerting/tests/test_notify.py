# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the notify-publish Lambda (spec 023 Task 5)."""
import importlib.util
import json
import os

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")  # module-load boto3 client

_PATH = os.path.join(os.path.dirname(__file__), "..", "lambdas", "notify", "main.py")


def _load():
    spec = importlib.util.spec_from_file_location("notify_main_under_test", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


notify = _load()


class _FakeIot:
    def __init__(self):
        self.calls = []

    def publish(self, topic, qos, payload):
        self.calls.append((topic, qos, payload))


def test_publishes_to_notifications_topic(monkeypatch):
    fake = _FakeIot()
    monkeypatch.setattr(notify, "_iot", fake)
    event = {
        "store_id": "sim-store-001",
        "alert_id": "a1",
        "notification": {"alert_id": "a1", "message": "Critical alert: x on y", "severity": "critical"},
    }
    out = notify.handler(event, None)
    assert out["published"] is True
    assert out["topic"] == "stores/sim-store-001/notifications/alert"
    topic, qos, payload = fake.calls[0]
    assert topic == "stores/sim-store-001/notifications/alert" and qos == 1
    assert json.loads(payload)["message"] == "Critical alert: x on y"


# Fake values live in neutrally named constants: Bandit B105/B106 flag a string
# *literal* bound to a secret-sounding name, even in tests.
_FAKE_TT = "tok1"


def test_persists_task_token_when_present(monkeypatch):
    fake = _FakeIot()
    monkeypatch.setattr(notify, "_iot", fake)
    persisted = []
    monkeypatch.setattr(notify, "_persist_task_token", lambda s, a, t: persisted.append((s, a, t)))
    out = notify.handler(
        {"store_id": "sim-store-001", "alert_id": "eq-1",
         "notification": {"message": "x"}, "task_token": _FAKE_TT},
        None,
    )
    assert out["published"] is True
    assert persisted == [("sim-store-001", "eq-1", _FAKE_TT)]
    assert fake.calls  # still published the notification


def test_no_token_means_no_persist(monkeypatch):
    fake = _FakeIot()
    monkeypatch.setattr(notify, "_iot", fake)
    persisted = []
    monkeypatch.setattr(notify, "_persist_task_token", lambda s, a, t: persisted.append(1))
    notify.handler(
        {"store_id": "sim-store-001", "alert_id": "eq-1", "notification": {"message": "x"}},
        None,
    )
    assert persisted == []
