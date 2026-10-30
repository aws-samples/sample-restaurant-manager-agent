# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for simulator.api.harness_tools (spec 022 Task 2)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from simulator.api.harness_tools import (
    INLINE_TOOLS,
    KNOWN_TOOLS,
    MAX_PARAM_JSON_BYTES,
    MAX_STRING_LEN,
    ToolParamError,
    _ToolConnection,
    execute_tool,
    sanitize_result,
    select_inline_tools,
    stream_collect,
    validate_params,
)
from simulator.mcp.registry import DOMAIN_OF_TOOL


# ---------------------------------------------------------------------------
# Inline tool specs cover exactly the 16 store tools
# ---------------------------------------------------------------------------


def test_inline_tools_cover_all_registry_tools() -> None:
    spec_names = {t["name"] for t in INLINE_TOOLS}
    assert spec_names == set(DOMAIN_OF_TOOL)
    assert len(INLINE_TOOLS) == 18  # 16 store tools + acknowledge_proactive_alert + get_active_alerts
    assert KNOWN_TOOLS == frozenset(DOMAIN_OF_TOOL)


def test_inline_tools_are_inline_function_specs() -> None:
    for t in INLINE_TOOLS:
        assert t["type"] == "inline_function"
        assert "inlineFunction" in t["config"]
        schema = t["config"]["inlineFunction"]["inputSchema"]
        assert schema["type"] == "object"


# ---------------------------------------------------------------------------
# validate_params (R2.4)
# ---------------------------------------------------------------------------


def test_validate_drops_unknown_keys() -> None:
    # TM-01: `store_id` is dropped alongside a plainly injected key. It used to
    # be allowed on every tool and pass through to topic routing, which let a
    # prompt-injected model aim a call at another tenant's store.
    cleaned = validate_params("get_equipment_status", {"store_id": "s1", "evil": "rm -rf"})
    assert cleaned == {}


def test_validate_unknown_tool_raises() -> None:
    with pytest.raises(ToolParamError, match="unknown tool"):
        validate_params("not_a_tool", {})


def test_validate_missing_required_raises() -> None:
    with pytest.raises(ToolParamError, match="missing required"):
        validate_params("void_order", {})
    # A blank string for a required STRING field is treated as missing.
    with pytest.raises(ToolParamError, match="missing required"):
        validate_params("void_order", {"order_id": 1, "reason": "   "})
    # `order_id` is declared `integer`, so a blank string there is now reported
    # as the type error it is (TM-03) rather than as "missing required" -- the
    # value is present, it is just not an integer. Checked here so the change in
    # which error surfaces is deliberate and documented, not incidental.
    with pytest.raises(ToolParamError, match="must be integer"):
        validate_params("void_order", {"order_id": "   ", "reason": "r"})


def test_validate_required_present_ok() -> None:
    cleaned = validate_params("void_order", {"order_id": 42, "reason": "customer_request", "store_id": "s1"})
    assert cleaned == {"order_id": 42, "reason": "customer_request"}  # store_id dropped (TM-01)


def test_validate_rejects_oversized_string() -> None:
    with pytest.raises(ToolParamError, match="too long"):
        validate_params("void_order", {"reason": "x" * (MAX_STRING_LEN + 1)})


def test_validate_rejects_control_chars() -> None:
    with pytest.raises(ToolParamError, match="control characters"):
        validate_params("void_order", {"reason": "ord\x00inject"})


def test_validate_rejects_oversized_payload() -> None:
    big = {"order_id": 1, "reason": "x" * (MAX_PARAM_JSON_BYTES)}
    # The size cap is checked before per-key validation, so this trips on bytes.
    with pytest.raises(ToolParamError, match="too large"):
        validate_params("void_order", big)


def test_validate_none_params_ok_for_no_required() -> None:
    assert validate_params("get_stock_levels", None) == {}


# ---------------------------------------------------------------------------
# sanitize_result (R2.4)
# ---------------------------------------------------------------------------


# Fake values live in neutrally named constants: Bandit B105/B106 flag a string
# *literal* bound to a secret-sounding name, even in tests.
_FAKE_KEY = "abc"
_FAKE_SV = "s"


def test_sanitize_redacts_sensitive_keys() -> None:
    out = sanitize_result(
        {"equipment": [{"id": "fryer_1", "api_key": _FAKE_KEY, "nested": {"secret": _FAKE_SV}}]}
    )
    assert out["equipment"][0]["id"] == "fryer_1"
    assert out["equipment"][0]["api_key"] == "<redacted>"
    assert out["equipment"][0]["nested"]["secret"] == "<redacted>"


def test_sanitize_truncates_long_strings() -> None:
    out = sanitize_result({"blob": "y" * 10000})
    assert out["blob"].endswith("...<truncated>")


def test_sanitize_passes_normal_data() -> None:
    data = {"temp_f": 351.4, "name": "Deep Fryer #1", "alerts": ["door_ajar"]}
    assert sanitize_result(data) == data


# ---------------------------------------------------------------------------
# stream_collect
# ---------------------------------------------------------------------------


def test_stream_collect_text_and_stop() -> None:
    resp = {
        "stream": [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"delta": {"text": "Hello "}}},
            {"contentBlockDelta": {"delta": {"text": "world"}}},
            {"messageStop": {"stopReason": "end_turn"}},
        ]
    }
    text, tu, stop = stream_collect(resp)
    assert text == "Hello world"
    assert stop == "end_turn"
    assert tu["id"] is None


def test_stream_collect_tool_use() -> None:
    resp = {
        "stream": [
            {"contentBlockStart": {"start": {"toolUse": {"toolUseId": "tu1", "name": "get_equipment_status"}}}},
            {"contentBlockDelta": {"delta": {"toolUse": {"input": '{"store'}}}},
            {"contentBlockDelta": {"delta": {"toolUse": {"input": '_id": "s1"}'}}}},
            {"messageStop": {"stopReason": "tool_use"}},
        ]
    }
    text, tu, stop = stream_collect(resp)
    assert stop == "tool_use"
    assert tu["id"] == "tu1"
    assert tu["name"] == "get_equipment_status"
    assert tu["input"] == '{"store_id": "s1"}'  # raw model output; validation happens later


def test_stream_collect_raises_on_runtime_error() -> None:
    resp = {"stream": [{"runtimeClientError": {"message": "boom"}}]}
    with pytest.raises(RuntimeError, match="harness runtime error"):
        stream_collect(resp)


# ---------------------------------------------------------------------------
# Proactive-ack tool (Task 11) + acknowledge_alert regression (2026-06-28)
# ---------------------------------------------------------------------------


def test_acknowledge_proactive_alert_requires_alert_id() -> None:
    cleaned = validate_params(
        "acknowledge_proactive_alert", {"alert_id": "equipment-freezer_1-x-1", "store_id": "s1"}
    )
    assert cleaned == {"alert_id": "equipment-freezer_1-x-1"}  # store_id dropped (TM-01)
    with pytest.raises(ToolParamError, match="missing required"):
        validate_params("acknowledge_proactive_alert", {})
    # equipment_id is not allowed on the proactive ack -> dropped silently.
    assert validate_params(
        "acknowledge_proactive_alert", {"alert_id": "a", "equipment_id": "fryer_1"}
    ) == {"alert_id": "a"}


def test_acknowledge_alert_requires_equipment_id() -> None:
    # Regression for the chat-ack bug: the tool takes equipment_id; a stray
    # alert_id is dropped, so the call must surface "missing equipment_id".
    with pytest.raises(ToolParamError, match="missing required"):
        validate_params("acknowledge_alert", {"alert_id": "equipment-freezer_1-x-1"})
    assert validate_params("acknowledge_alert", {"equipment_id": "fryer_1"}) == {"equipment_id": "fryer_1"}


def test_proactive_ack_inline_spec_present() -> None:
    spec = next(t for t in INLINE_TOOLS if t["name"] == "acknowledge_proactive_alert")
    schema = spec["config"]["inlineFunction"]["inputSchema"]
    assert "alert_id" in schema["properties"]
    assert schema["required"] == ["alert_id"]


# ---------------------------------------------------------------------------
# Pooled Correlation-A round-trip (_ToolConnection) — AGENTPERF06 connection
# reuse. The native transport is faked so routing/timeout/reuse/reset logic is
# covered without awscrt/awsiot.
# ---------------------------------------------------------------------------


class _FakeTransport:
    def __init__(self, *, store_id, iot_endpoint, region, responder=None, fail_start=False):
        self.store_id = store_id
        self.iot_endpoint = iot_endpoint
        self.region = region
        self._responder = responder
        self._fail_start = fail_start
        self.on_message = None
        self.started = 0
        self.closed = 0
        self.published = []

    def start(self, resp_filter, on_message):
        if self._fail_start:
            raise RuntimeError("connect failed")
        self.resp_filter = resp_filter
        self.on_message = on_message
        self.started += 1

    def publish(self, topic, payload):
        self.published.append((topic, payload))
        cmd = json.loads(payload.decode("utf-8"))
        env = self._responder(cmd) if self._responder else None
        if env is not None:
            cid = cmd["correlation_id"]
            self.on_message(
                f"stores/{self.store_id}/responses/{cid}", json.dumps(env).encode("utf-8")
            )

    def close(self):
        self.closed += 1


def _make_factory(**kwargs):
    created = []

    def factory(*, store_id, iot_endpoint, region):
        t = _FakeTransport(store_id=store_id, iot_endpoint=iot_endpoint, region=region, **kwargs)
        created.append(t)
        return t

    factory.created = created
    return factory


def _success_responder(cmd):
    return {"status": "success", "result": {"tool": cmd["tool_name"], "ok": True}}


def test_round_trip_routes_response_to_waiter() -> None:
    factory = _make_factory(responder=_success_responder)
    conn = _ToolConnection(store_id="s1", iot_endpoint="ep", region="us-west-2", transport_factory=factory)
    env = conn.round_trip(tool_name="get_daily_sales", domain="pos", parameters={}, timeout_s=2.0)
    assert env == {"status": "success", "result": {"tool": "get_daily_sales", "ok": True}}
    (topic, payload), = factory.created[0].published
    assert topic == "stores/s1/commands/pos"
    assert json.loads(payload.decode())["tool_name"] == "get_daily_sales"


def test_round_trip_times_out_without_response() -> None:
    factory = _make_factory(responder=lambda cmd: None)
    conn = _ToolConnection(store_id="s1", iot_endpoint="ep", region="r", transport_factory=factory)
    env = conn.round_trip(tool_name="get_stock_levels", domain="inventory", parameters={}, timeout_s=0.2)
    assert env["status"] == "error" and env["error"]["code"] == "timeout"


def test_connection_is_reused_across_calls() -> None:
    factory = _make_factory(responder=_success_responder)
    conn = _ToolConnection(store_id="s1", iot_endpoint="ep", region="r", transport_factory=factory)
    conn.round_trip(tool_name="get_daily_sales", domain="pos", parameters={}, timeout_s=2.0)
    conn.round_trip(tool_name="get_equipment_status", domain="equipment", parameters={}, timeout_s=2.0)
    assert len(factory.created) == 1
    assert factory.created[0].started == 1
    assert len(factory.created[0].published) == 2


def test_transport_error_resets_and_reconnects() -> None:
    created = []

    def factory(*, store_id, iot_endpoint, region):
        t = _FakeTransport(
            store_id=store_id, iot_endpoint=iot_endpoint, region=region,
            responder=_success_responder, fail_start=(len(created) == 0),
        )
        created.append(t)
        return t

    conn = _ToolConnection(store_id="s1", iot_endpoint="ep", region="r", transport_factory=factory)
    env1 = conn.round_trip(tool_name="get_daily_sales", domain="pos", parameters={}, timeout_s=1.0)
    assert env1["status"] == "error" and env1["error"]["code"] == "transport_error"
    assert conn._transport is None
    env2 = conn.round_trip(tool_name="get_daily_sales", domain="pos", parameters={}, timeout_s=2.0)
    assert env2["status"] == "success"
    assert len(created) == 2


def test_unparseable_response_becomes_error_envelope() -> None:
    class _BadTransport(_FakeTransport):
        def publish(self, topic, payload):
            self.published.append((topic, payload))
            cid = json.loads(payload.decode())["correlation_id"]
            self.on_message(f"stores/{self.store_id}/responses/{cid}", b"not json{{")

    def factory(*, store_id, iot_endpoint, region):
        return _BadTransport(store_id=store_id, iot_endpoint=iot_endpoint, region=region)

    conn = _ToolConnection(store_id="s1", iot_endpoint="ep", region="r", transport_factory=factory)
    env = conn.round_trip(tool_name="get_daily_sales", domain="pos", parameters={}, timeout_s=2.0)
    assert env["status"] == "error" and env["error"]["code"] == "unparseable"


def test_late_response_for_unknown_cid_is_ignored() -> None:
    conn = _ToolConnection(store_id="s1", iot_endpoint="ep", region="r", transport_factory=_make_factory())
    conn._on_message("stores/s1/responses/nope", json.dumps({"status": "success"}).encode())


def test_execute_tool_unknown_tool_and_not_configured() -> None:
    assert execute_tool(tool_name="nope", parameters={}, store_id="s1", iot_endpoint="ep", region="r")["error"]["code"] == "unknown_tool"
    assert execute_tool(tool_name="get_daily_sales", parameters={}, store_id="s1", iot_endpoint="", region="r")["error"]["code"] == "not_configured"


def test_execute_tool_uses_pool_and_sanitizes(monkeypatch) -> None:
    fake_conn = _ToolConnection(
        store_id="s1", iot_endpoint="ep", region="r",
        transport_factory=_make_factory(responder=lambda cmd: {"status": "success", "result": {"value": 1, "api_key": "leak"}}),
    )
    monkeypatch.setattr("simulator.api.harness_tools._get_pooled_connection", lambda *a, **k: fake_conn)
    out = execute_tool(tool_name="get_daily_sales", parameters={}, store_id="s1", iot_endpoint="ep", region="r")
    assert out["status"] == "success"
    assert out["result"]["value"] == 1
    assert out["result"]["api_key"] == "<redacted>"


# ---------------------------------------------------------------------------
# Per-tool CloudWatch telemetry (_emit_tool_metric) — AGENTPERF06. Gated on
# TOOL_METRICS_NAMESPACE, off the critical path; CloudWatch is faked.
# ---------------------------------------------------------------------------


class _FakeCw:
    def __init__(self):
        self.calls = []

    def put_metric_data(self, **kwargs):
        self.calls.append(kwargs)


def test_emit_tool_metric_noop_without_namespace(monkeypatch) -> None:
    import simulator.api.harness_tools as ht

    monkeypatch.delenv("TOOL_METRICS_NAMESPACE", raising=False)
    touched = []
    monkeypatch.setattr(ht, "_get_cw_client", lambda region: touched.append(region))
    monkeypatch.setattr(ht, "_run_async", lambda fn: touched.append("ran"))
    ht._emit_tool_metric(tool_name="get_daily_sales", outcome="success", latency_ms=1.0, region="r")
    assert touched == []  # no client, no dispatch when disabled


def test_emit_tool_metric_puts_when_enabled(monkeypatch) -> None:
    import simulator.api.harness_tools as ht

    monkeypatch.setenv("TOOL_METRICS_NAMESPACE", "RestaurantSim/AgentTools")
    monkeypatch.setattr(ht, "_run_async", lambda fn: fn())  # run inline
    fake = _FakeCw()
    monkeypatch.setattr(ht, "_get_cw_client", lambda region: fake)
    ht._emit_tool_metric(
        tool_name="get_daily_sales", outcome="success", latency_ms=12.0, region="r", store_duration_ms=5
    )
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["Namespace"] == "RestaurantSim/AgentTools"
    names = {m["MetricName"] for m in call["MetricData"]}
    assert {"ToolInvocations", "ToolLatencyMs", "ToolStoreDurationMs"} <= names
    inv = next(m for m in call["MetricData"] if m["MetricName"] == "ToolInvocations")
    assert {"Name": "Outcome", "Value": "success"} in inv["Dimensions"]
    assert {"Name": "Tool", "Value": "get_daily_sales"} in inv["Dimensions"]


def test_emit_tool_metric_timeout_adds_timeout_metric(monkeypatch) -> None:
    import simulator.api.harness_tools as ht

    monkeypatch.setenv("TOOL_METRICS_NAMESPACE", "RestaurantSim/AgentTools")
    monkeypatch.setattr(ht, "_run_async", lambda fn: fn())
    fake = _FakeCw()
    monkeypatch.setattr(ht, "_get_cw_client", lambda region: fake)
    ht._emit_tool_metric(tool_name="get_stock_levels", outcome="timeout", latency_ms=30000.0, region="r")
    data = fake.calls[0]["MetricData"]
    names = {m["MetricName"] for m in data}
    assert "ToolTimeouts" in names
    assert "ToolTimeoutsTotal" in names  # dimensionless aggregate for the (non-SEARCH) alarm
    assert "ToolStoreDurationMs" not in names  # no store duration on a timeout
    # The aggregate is dimensionless -> a single scalar series a Metric Alarm can use.
    total = next(m for m in data if m["MetricName"] == "ToolTimeoutsTotal")
    assert total["Dimensions"] == []
    # ...and the per-tool metric keeps the Tool dimension.
    per_tool = next(m for m in data if m["MetricName"] == "ToolTimeouts")
    assert {"Name": "Tool", "Value": "get_stock_levels"} in per_tool["Dimensions"]


def test_execute_tool_emits_outcome_metrics(monkeypatch) -> None:
    import simulator.api.harness_tools as ht

    recorded = []
    monkeypatch.setattr(ht, "_emit_tool_metric", lambda **kw: recorded.append(kw))

    monkeypatch.setattr(
        ht, "_get_pooled_connection",
        lambda *a, **k: _ToolConnection(
            store_id="s1", iot_endpoint="ep", region="r",
            transport_factory=_make_factory(responder=_success_responder),
        ),
    )
    execute_tool(tool_name="get_daily_sales", parameters={}, store_id="s1", iot_endpoint="ep", region="r")
    assert recorded[-1]["outcome"] == "success"
    assert recorded[-1]["tool_name"] == "get_daily_sales"

    monkeypatch.setattr(
        ht, "_get_pooled_connection",
        lambda *a, **k: _ToolConnection(
            store_id="s1", iot_endpoint="ep", region="r",
            transport_factory=_make_factory(responder=lambda cmd: None),
        ),
    )
    execute_tool(tool_name="get_stock_levels", parameters={}, store_id="s1", iot_endpoint="ep", region="r", timeout_s=0.2)
    assert recorded[-1]["outcome"] == "timeout"


# ---------------------------------------------------------------------------
# StoreStateCache read fallback (spec 026) — classification + helpers + the
# execute_tool write-through / fallback wiring. DynamoDB is faked.
# ---------------------------------------------------------------------------


class _FakeDdbTable:
    def __init__(self):
        self.items: dict[str, dict] = {}
        self.puts: list[dict] = []

    def put_item(self, *, Item):  # noqa: N803 (boto3 kwarg name)
        self.puts.append(Item)
        self.items[Item["store_domain_key"]] = Item

    def get_item(self, *, Key):  # noqa: N803
        item = self.items.get(Key["store_domain_key"])
        return {"Item": item} if item is not None else {}


def _enable_cache(monkeypatch, ht, table) -> None:
    monkeypatch.setenv("TOOL_READ_CACHE_ENABLED", "true")
    monkeypatch.setenv("STORE_STATE_CACHE_TABLE", "StoreStateCache")
    monkeypatch.setattr(ht, "_get_ddb_table", lambda region: table)
    monkeypatch.setattr(ht, "_run_async", lambda fn: fn())  # write-through inline
    monkeypatch.setattr(ht, "_emit_tool_metric", lambda **kw: None)


def test_cacheable_read_tools_are_the_parameterless_reads() -> None:
    import simulator.api.harness_tools as ht

    assert ht.CACHEABLE_READ_TOOLS == frozenset(
        {
            "get_daily_sales",
            "get_active_orders",
            "get_equipment_status",
            "get_active_alerts",
            "get_stock_levels",
            "check_reorder_status",
            "get_current_shifts",
            "get_labor_cost",
        }
    )
    for excluded in (
        "void_order", "set_timer", "acknowledge_alert", "acknowledge_proactive_alert",
        "log_waste", "clock_in", "clock_out",                       # mutating
        "get_temperature_log", "get_transaction_history", "get_usage_report",  # parameterized
    ):
        assert excluded not in ht.CACHEABLE_READ_TOOLS


def test_cache_write_and_read_round_trip(monkeypatch) -> None:
    import simulator.api.harness_tools as ht

    table = _FakeDdbTable()
    _enable_cache(monkeypatch, ht, table)
    ht._cache_write("s1", "get_equipment_status", "r", {"temp_f": 38})
    assert table.puts[0]["store_domain_key"] == "s1#get_equipment_status"
    assert json.loads(table.puts[0]["result_json"]) == {"temp_f": 38}
    out = ht._cache_read("s1", "get_equipment_status", "r")
    assert out["result"] == {"temp_f": 38}
    assert out["age_seconds"] >= 0 and "as_of" in out


def test_cache_read_miss_returns_none(monkeypatch) -> None:
    import simulator.api.harness_tools as ht

    _enable_cache(monkeypatch, ht, _FakeDdbTable())
    assert ht._cache_read("s1", "get_stock_levels", "r") is None


def test_cache_read_too_stale_returns_none(monkeypatch) -> None:
    import simulator.api.harness_tools as ht

    table = _FakeDdbTable()
    _enable_cache(monkeypatch, ht, table)
    monkeypatch.setenv("TOOL_READ_CACHE_MAX_STALENESS_S", "60")
    old = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    table.items["s1#get_daily_sales"] = {
        "store_domain_key": "s1#get_daily_sales", "result_json": "{}", "cached_at": old,
    }
    assert ht._cache_read("s1", "get_daily_sales", "r") is None


def _conn_factory(ht, responder):
    return lambda *a, **k: _ToolConnection(
        store_id="s1", iot_endpoint="ep", region="r",
        transport_factory=_make_factory(responder=responder),
    )


def test_execute_tool_writes_through_on_cacheable_success(monkeypatch) -> None:
    import simulator.api.harness_tools as ht

    table = _FakeDdbTable()
    _enable_cache(monkeypatch, ht, table)
    monkeypatch.setattr(ht, "_get_pooled_connection",
                        _conn_factory(ht, lambda cmd: {"status": "success", "result": {"ok": 1}}))
    out = execute_tool(tool_name="get_equipment_status", parameters={}, store_id="s1", iot_endpoint="ep", region="r")
    assert out["status"] == "success" and "stale" not in out
    assert table.puts and table.puts[0]["store_domain_key"] == "s1#get_equipment_status"


def test_execute_tool_no_writethrough_for_non_cacheable(monkeypatch) -> None:
    import simulator.api.harness_tools as ht

    table = _FakeDdbTable()
    _enable_cache(monkeypatch, ht, table)
    monkeypatch.setattr(ht, "_get_pooled_connection",
                        _conn_factory(ht, lambda cmd: {"status": "success", "result": {"ok": 1}}))
    # get_temperature_log is a parameterized read -> not cacheable
    execute_tool(tool_name="get_temperature_log", parameters={"equipment_id": "fryer_1"},
                 store_id="s1", iot_endpoint="ep", region="r")
    assert table.puts == []


def test_execute_tool_fallback_on_timeout_returns_stale(monkeypatch) -> None:
    import simulator.api.harness_tools as ht

    table = _FakeDdbTable()
    _enable_cache(monkeypatch, ht, table)
    table.items["s1#get_equipment_status"] = {
        "store_domain_key": "s1#get_equipment_status",
        "result_json": json.dumps({"temp_f": 38}),
        "cached_at": datetime.now(timezone.utc).isoformat(),
    }
    monkeypatch.setattr(ht, "_get_pooled_connection", _conn_factory(ht, lambda cmd: None))  # never replies
    out = execute_tool(tool_name="get_equipment_status", parameters={}, store_id="s1",
                       iot_endpoint="ep", region="r", timeout_s=0.2)
    assert out["status"] == "success" and out["stale"] is True
    assert out["result"] == {"temp_f": 38} and out["source"] == "cache" and out["age_seconds"] >= 0


def test_execute_tool_fallback_skipped_when_disabled(monkeypatch) -> None:
    import simulator.api.harness_tools as ht

    monkeypatch.delenv("TOOL_READ_CACHE_ENABLED", raising=False)
    monkeypatch.setattr(ht, "_emit_tool_metric", lambda **kw: None)
    monkeypatch.setattr(ht, "_get_pooled_connection", _conn_factory(ht, lambda cmd: None))
    out = execute_tool(tool_name="get_equipment_status", parameters={}, store_id="s1",
                       iot_endpoint="ep", region="r", timeout_s=0.2)
    assert out["status"] == "error" and out["error"]["code"] == "timeout"


def test_execute_tool_fallback_skipped_for_mutating_tool(monkeypatch) -> None:
    import simulator.api.harness_tools as ht

    table = _FakeDdbTable()
    _enable_cache(monkeypatch, ht, table)
    # even if an entry somehow exists for a mutating tool, it must never be served
    table.items["s1#set_timer"] = {
        "store_domain_key": "s1#set_timer", "result_json": "{}",
        "cached_at": datetime.now(timezone.utc).isoformat(),
    }
    monkeypatch.setattr(ht, "_get_pooled_connection", _conn_factory(ht, lambda cmd: None))
    out = execute_tool(tool_name="set_timer", parameters={"equipment_id": "fryer_1", "minutes": 5},
                       store_id="s1", iot_endpoint="ep", region="r", timeout_s=0.2)
    assert out["status"] == "error"


def test_execute_tool_live_success_not_overridden_by_cache(monkeypatch) -> None:
    import simulator.api.harness_tools as ht

    table = _FakeDdbTable()
    _enable_cache(monkeypatch, ht, table)
    # stale cache present, but the live read succeeds -> live result wins
    table.items["s1#get_stock_levels"] = {
        "store_domain_key": "s1#get_stock_levels",
        "result_json": json.dumps({"old": True}),
        "cached_at": datetime.now(timezone.utc).isoformat(),
    }
    monkeypatch.setattr(ht, "_get_pooled_connection",
                        _conn_factory(ht, lambda cmd: {"status": "success", "result": {"fresh": True}}))
    out = execute_tool(tool_name="get_stock_levels", parameters={}, store_id="s1", iot_endpoint="ep", region="r")
    assert out["result"] == {"fresh": True} and "stale" not in out


# ---------------------------------------------------------------------------
# Text tool-catalog filter (spec 027) — select_inline_tools. Default OFF;
# domain-keyword selection + core set; full-catalog fallback when ambiguous.
# ---------------------------------------------------------------------------


def test_select_inline_tools_disabled_returns_all(monkeypatch) -> None:
    monkeypatch.delenv("TOOL_CATALOG_FILTER_ENABLED", raising=False)
    assert select_inline_tools("how were sales today?") is INLINE_TOOLS


def test_select_inline_tools_single_domain(monkeypatch) -> None:
    monkeypatch.setenv("TOOL_CATALOG_FILTER_ENABLED", "true")
    tools = select_inline_tools("how were sales and revenue today?")
    names = {t["name"] for t in tools}
    assert "get_daily_sales" in names and "get_active_orders" in names  # pos
    assert "get_active_alerts" in names  # core always present
    assert "clock_in" not in names and "get_stock_levels" not in names  # other domains excluded
    assert len(tools) <= 10


def test_select_inline_tools_multi_domain(monkeypatch) -> None:
    monkeypatch.setenv("TOOL_CATALOG_FILTER_ENABLED", "true")
    tools = select_inline_tools("is the fryer ok and how were sales?")
    names = {t["name"] for t in tools}
    assert "get_equipment_status" in names and "get_daily_sales" in names


def test_select_inline_tools_ambiguous_returns_all(monkeypatch) -> None:
    monkeypatch.setenv("TOOL_CATALOG_FILTER_ENABLED", "true")
    assert select_inline_tools("how are things looking?") is INLINE_TOOLS


def test_select_inline_tools_empty_message_returns_all(monkeypatch) -> None:
    monkeypatch.setenv("TOOL_CATALOG_FILTER_ENABLED", "true")
    assert select_inline_tools("") is INLINE_TOOLS


def test_select_inline_tools_preserves_order_and_identity(monkeypatch) -> None:
    monkeypatch.setenv("TOOL_CATALOG_FILTER_ENABLED", "true")
    tools = select_inline_tools("check the freezer temperature")  # equipment
    idx = [INLINE_TOOLS.index(t) for t in tools]
    assert idx == sorted(idx)  # order preserved
    assert all(any(t is orig for orig in INLINE_TOOLS) for t in tools)  # same objects
