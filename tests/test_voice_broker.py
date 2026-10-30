# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the voice broker's Task 1a hardening (spec 025):
custom credentials resolver + dual-path tool dispatch (gateway round-trip / local fallback).
"""
import json

from simulator.voice import broker

# Fake values live in neutrally named constants: Bandit B105/B106 flag a string
# *literal* bound to a secret-sounding name, even in tests.
_FAKE_SK = "secretX"
_FAKE_ST = "tok123"


class _FrozenCreds:
    access_key = "AKIATEST"
    secret_key = _FAKE_SK
    token = _FAKE_ST


class _Creds:
    def get_frozen_credentials(self):
        return _FrozenCreds()


class _FakeSession:
    def get_credentials(self):
        return _Creds()


async def test_backend_credentials_resolver_returns_identity():
    r = broker._BackendCredentialsResolver()
    r._session = _FakeSession()
    ident = await r.get_identity(properties=None)
    assert ident.access_key_id == "AKIATEST"
    assert ident.secret_access_key == _FAKE_SK
    assert ident.session_token == _FAKE_ST


async def test_run_tool_invalid_params():
    # set_timer requires equipment_id + minutes -> validation rejects {}
    out = await broker._run_tool("set_timer", "{}")
    assert out["status"] == "error"
    assert out["error"]["code"] == "invalid_params"


async def test_run_tool_dev_registry_drops_store_id(monkeypatch):
    monkeypatch.delenv("IOT_ENDPOINT", raising=False)
    seen = {}

    async def fake_get_equipment_status(**kw):
        seen.update(kw)
        return {"equipment": []}

    out = await broker._run_tool(
        "get_equipment_status",
        json.dumps({"store_id": "sim-store-001"}),
        registry={"get_equipment_status": fake_get_equipment_status},
    )
    assert out["status"] == "success"
    assert "store_id" not in seen  # dropped before the local call


async def test_run_tool_unknown_tool_in_dev(monkeypatch):
    monkeypatch.delenv("IOT_ENDPOINT", raising=False)
    out = await broker._run_tool("get_equipment_status", "{}", registry={})
    assert out["status"] == "error"
    assert out["error"]["code"] == "unknown_tool"


async def test_run_tool_prod_uses_gateway_round_trip(monkeypatch):
    monkeypatch.setenv("IOT_ENDPOINT", "xxxx-ats.iot.us-west-2.amazonaws.com")
    monkeypatch.setenv("STORE_ID", "sim-store-001")
    calls = {}

    def fake_execute_tool(*, tool_name, parameters, store_id, iot_endpoint, region, timeout_s=30.0):
        calls.update(tool_name=tool_name, parameters=parameters, store_id=store_id,
                     iot_endpoint=iot_endpoint, region=region)
        return {"status": "success", "result": {"equipment": []}}

    import simulator.api.harness_tools as ht
    monkeypatch.setattr(ht, "execute_tool", fake_execute_tool)

    out = await broker._run_tool("get_equipment_status", json.dumps({"store_id": "sim-store-001"}))
    assert out["status"] == "success"
    assert calls["tool_name"] == "get_equipment_status"
    assert calls["store_id"] == "sim-store-001"
    assert "store_id" not in calls["parameters"]  # routing only, not a tool param
    assert calls["iot_endpoint"].endswith("amazonaws.com")


def test_compose_alert_text_includes_details_and_ack_tool():
    txt = broker._compose_alert_text({
        "severity": "warning", "source_id": "fryer_1", "condition": "over_temp",
        "reading": {"temp_f": 410}, "alert_id": "equipment-fryer_1-over_temp-1",
    })
    assert "warning" in txt and "fryer_1" in txt and "over_temp" in txt
    assert "equipment-fryer_1-over_temp-1" in txt
    assert "acknowledge_proactive_alert" in txt


async def test_speak_alert_injects_cross_modal_text_turn():
    sess = broker._SonicSession()
    sent = []

    async def rec(payload):
        sent.append(json.loads(payload))

    sess._send = rec
    await sess.speak_alert({
        "severity": "critical", "source_id": "freezer_1", "condition": "thermostat_malfunction",
        "reading": {"temp_f": 28}, "alert_id": "equipment-freezer_1-thermostat_malfunction-1980366",
    })
    kinds = [next(iter(s["event"])) for s in sent]
    assert kinds == ["contentStart", "textInput", "contentEnd"]
    assert sent[0]["event"]["contentStart"]["type"] == "TEXT"
    assert sent[0]["event"]["contentStart"]["role"] == "USER"
    content = sent[1]["event"]["textInput"]["content"]
    assert "freezer_1" in content and "critical" in content
    assert "equipment-freezer_1-thermostat_malfunction-1980366" in content


def test_history_payloads_builds_conversation_block():
    sess = broker._SonicSession()
    out = sess._history_payloads([
        {"role": "user", "content": "how are sales today?"},
        {"role": "assistant", "content": "Sales are $1,234 so far."},
        {"role": "error", "content": "ignored"},   # invalid role -> skipped
        {"role": "user", "content": "   "},          # empty -> skipped
    ])
    events = [json.loads(p) for p in out]
    kinds = [next(iter(e["event"])) for e in events]
    assert kinds == ["contentStart", "textInput", "contentEnd",
                     "contentStart", "textInput", "contentEnd"]  # 2 valid turns only
    assert events[0]["event"]["contentStart"]["role"] == "USER"
    assert events[0]["event"]["contentStart"]["type"] == "TEXT"
    assert events[3]["event"]["contentStart"]["role"] == "ASSISTANT"
    assert events[1]["event"]["textInput"]["content"] == "how are sales today?"


def test_history_payloads_empty():
    sess = broker._SonicSession()
    assert sess._history_payloads(None) == []
    assert sess._history_payloads([]) == []


def test_history_payloads_skips_raw_tool_use_records():
    sess = broker._SonicSession()
    raw_record = (
        '{"message": {"role": "assistant", "content": [{"toolUse": {"name": '
        '"get_equipment_status", "toolUseId": "tooluse_x", "input": {}}}]}, '
        '"message_id": 0, "redact_message": null, "created_at": "2026-06-28T13:25:50"}'
    )
    out = sess._history_payloads([
        {"role": "user", "content": "what's the fryer temperature?"},
        {"role": "assistant", "content": raw_record},          # tool-use record -> skipped
        {"role": "assistant", "content": '[{"text": "blocks"}]'},  # JSON-looking -> skipped
        {"role": "assistant", "content": "The fryer is at 350F."},
    ])
    events = [json.loads(p) for p in out]
    kinds = [next(iter(e["event"])) for e in events]
    assert kinds == ["contentStart", "textInput", "contentEnd",
                     "contentStart", "textInput", "contentEnd"]  # only the 2 prose turns
    texts = [e["event"]["textInput"]["content"] for e in events if "textInput" in e["event"]]
    assert texts == ["what's the fryer temperature?", "The fryer is at 350F."]
    assert all("toolUse" not in t and "message_id" not in t for t in texts)


def test_history_payloads_drops_leading_assistant_turn():
    """Nova Sonic rejects a seeded history that starts with an assistant turn.

    Regression guard for the 2026-08-05 voice outage: the session failed to open
    with ``ValidationException: First message in chat history should not be
    Assistant.``, so the browser showed "connecting" and fell back to idle with
    no audio at all. Reachable whenever Memory's first turn is an assistant
    message -- e.g. a proactive alert the display spoke before the manager said
    anything.
    """
    sess = broker._SonicSession()
    out = sess._history_payloads([
        {"role": "assistant", "content": "Heads up: the walk-in cooler is warm."},
        {"role": "user", "content": "what should I do?"},
        {"role": "assistant", "content": "Check the door seal."},
    ])
    events = [json.loads(p) for p in out]
    roles = [
        e["event"]["contentStart"]["role"] for e in events if "contentStart" in e["event"]
    ]
    assert roles and roles[0] == "USER", "history must not begin with ASSISTANT"
    assert roles == ["USER", "ASSISTANT"]
    texts = [e["event"]["textInput"]["content"] for e in events if "textInput" in e["event"]]
    assert texts == ["what should I do?", "Check the door seal."]


def test_history_payloads_drops_assistant_promoted_by_filtering():
    """The leading USER turn being filtered out must not promote an ASSISTANT.

    The subtle path into the same outage: the first user turn is dropped as
    empty or record-shaped, leaving an assistant turn first.
    """
    sess = broker._SonicSession()
    raw_record = '{"message": {"role": "user", "content": []}, "message_id": 3}'
    out = sess._history_payloads([
        {"role": "user", "content": raw_record},        # record-shaped -> dropped
        {"role": "assistant", "content": "Sales are $1,234."},
        {"role": "user", "content": "and labour cost?"},
    ])
    events = [json.loads(p) for p in out]
    roles = [
        e["event"]["contentStart"]["role"] for e in events if "contentStart" in e["event"]
    ]
    assert roles == ["USER"], f"expected only the trailing user turn, got {roles}"
    texts = [e["event"]["textInput"]["content"] for e in events if "textInput" in e["event"]]
    assert texts == ["and labour cost?"]


def test_history_payloads_assistant_only_history_seeds_nothing():
    """An all-assistant history seeds nothing rather than failing the session."""
    sess = broker._SonicSession()
    assert sess._history_payloads([
        {"role": "assistant", "content": "Alert: freezer door open."},
        {"role": "assistant", "content": "Alert: freezer still open."},
    ]) == []


def test_sonic_tools_is_curated_voice_set():
    names = {t["toolSpec"]["name"] for t in broker._sonic_tools()}
    assert names == broker.VOICE_TOOL_NAMES        # exactly the curated set
    assert "get_active_alerts" in names            # the alert-listing tool is offered
    assert len(names) <= 8                          # kept small for Nova Sonic reliability
