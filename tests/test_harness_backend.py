# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the harness chat backend + route backend selection
(spec 022 Task 2)."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from simulator.api.chat_service import ChatInvokeResult, ChatServiceError
from simulator.api.harness_backend import HarnessChatBackend

HARNESS_ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:harness/restaurant_manager_agent-EXAMPLE123"


# ---------------------------------------------------------------------------
# Stream builders + helpers
# ---------------------------------------------------------------------------


def _tool_use_stream(tool_use_id: str, name: str, input_str: str = "{}") -> dict:
    return {
        "stream": [
            {"contentBlockStart": {"start": {"toolUse": {"toolUseId": tool_use_id, "name": name}}}},
            {"contentBlockDelta": {"delta": {"toolUse": {"input": input_str}}}},
            {"messageStop": {"stopReason": "tool_use"}},
        ]
    }


def _text_stream(text: str) -> dict:
    return {
        "stream": [
            {"contentBlockDelta": {"delta": {"text": text}}},
            {"messageStop": {"stopReason": "end_turn"}},
        ]
    }


def _make_backend(tool_executor=None, **overrides) -> HarnessChatBackend:
    kwargs = dict(
        region="us-west-2",
        harness_arn=HARNESS_ARN,
        memory_id="mem-1",
        store_id="sim-store-001",
        iot_endpoint="endpoint-ats.iot.us-west-2.amazonaws.com",
        tool_executor=tool_executor,
    )
    kwargs.update(overrides)
    svc = HarnessChatBackend(**kwargs)
    svc._agentcore_client = MagicMock()
    return svc


def _wait(svc: HarnessChatBackend, exec_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = svc.get_execution_result(exec_id, None)
        if result["status"] != "in_progress":
            return result
        time.sleep(0.02)
    raise AssertionError("background turn loop did not complete in time")


# ---------------------------------------------------------------------------
# Configuration gates
# ---------------------------------------------------------------------------


def test_configured_and_memory_configured() -> None:
    svc = _make_backend()
    assert svc.configured is True
    assert svc.memory_configured is True
    bare = HarnessChatBackend(region="us-west-2", harness_arn=None, memory_id="  ")
    assert bare.configured is False
    assert bare.memory_configured is False


def test_invoke_raises_when_not_configured() -> None:
    svc = HarnessChatBackend(region="us-west-2", harness_arn=None, memory_id=None)
    with pytest.raises(ChatServiceError) as exc:
        svc.invoke("hello")
    assert exc.value.error_kind == "supervisor_not_configured"


def test_session_id_min_length() -> None:
    svc = _make_backend()
    # The named constant, not a magic 33: the generator's format may change
    # (it happens to yield 45 chars), but the AgentCore minimum is the contract.
    from simulator.api.harness_backend import MIN_RUNTIME_SESSION_ID_LENGTH

    for _ in range(20):
        assert len(svc.generate_session_id()) >= MIN_RUNTIME_SESSION_ID_LENGTH


# ---------------------------------------------------------------------------
# Inline tool loop
# ---------------------------------------------------------------------------


def test_inline_loop_executes_tool_and_returns_final_answer() -> None:
    executor = MagicMock(return_value={"status": "success", "result": {"equipment": [{"id": "fryer_1"}]}})
    svc = _make_backend(tool_executor=executor)
    # invoke 1 -> tool_use, invoke 2 -> final text.
    svc._agentcore_client.invoke_harness.side_effect = [
        _tool_use_stream("tu1", "get_equipment_status", '{"store_id": "store-xyz"}'),
        _text_stream("Deep Fryer #1 is operating normally."),
    ]

    result = svc.invoke("What is the equipment status?", session_id="sess-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123")
    assert isinstance(result, ChatInvokeResult)
    assert result.session_id == "sess-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"

    final = _wait(svc, result.execution_id)
    assert final["status"] == "succeeded"
    assert "Deep Fryer #1" in final["response"]
    assert final["session_id"] == "sess-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"

    # TM-01: the model emitted {"store_id": "store-xyz"} in the tool input. That
    # value must be IGNORED -- the call routes to the backend's configured store.
    # This assertion previously required "store-xyz", i.e. it encoded the
    # cross-tenant bug as expected behaviour.
    executor.assert_called_once()
    call = executor.call_args.kwargs
    assert call["tool_name"] == "get_equipment_status"
    assert call["store_id"] == "sim-store-001"
    assert "store_id" not in call["parameters"]


def test_inline_loop_reinvokes_with_tool_result_messages() -> None:
    executor = MagicMock(return_value={"status": "success", "result": {"ok": True}})
    svc = _make_backend(tool_executor=executor)
    svc._agentcore_client.invoke_harness.side_effect = [
        _tool_use_stream("tu9", "get_daily_sales", "{}"),
        _text_stream("Sales today were $1,234."),
    ]

    result = svc.invoke("sales?")
    _wait(svc, result.execution_id)

    # Second invoke must carry the assistant toolUse + user toolResult.
    second_call = svc._agentcore_client.invoke_harness.call_args_list[1]
    messages = second_call.kwargs["messages"]
    assert messages[0]["role"] == "assistant"
    assert messages[0]["content"][0]["toolUse"]["toolUseId"] == "tu9"
    assert messages[1]["role"] == "user"
    tr = messages[1]["content"][0]["toolResult"]
    assert tr["toolUseId"] == "tu9"
    assert tr["status"] == "success"


def test_invalid_tool_params_return_error_result_without_executing() -> None:
    executor = MagicMock()
    svc = _make_backend(tool_executor=executor)
    # void_order requires order_id; the model omits it.
    svc._agentcore_client.invoke_harness.side_effect = [
        _tool_use_stream("tu2", "void_order", "{}"),
        _text_stream("I could not void that order."),
    ]

    result = svc.invoke("void it")
    final = _wait(svc, result.execution_id)
    assert final["status"] == "succeeded"
    # Executor never called because validation failed first.
    executor.assert_not_called()
    # The toolResult sent back to the harness carries an error status.
    second_call = svc._agentcore_client.invoke_harness.call_args_list[1]
    tr = second_call.kwargs["messages"][1]["content"][0]["toolResult"]
    assert tr["status"] == "error"


def test_no_tool_turn_returns_text_immediately() -> None:
    svc = _make_backend()
    svc._agentcore_client.invoke_harness.side_effect = [_text_stream("Hi there!")]
    result = svc.invoke("hello")
    final = _wait(svc, result.execution_id)
    assert final["status"] == "succeeded"
    assert final["response"] == "Hi there!"
    assert svc._agentcore_client.invoke_harness.call_count == 1


def test_runtime_error_marks_task_failed() -> None:
    svc = _make_backend()
    svc._agentcore_client.invoke_harness.side_effect = [
        {"stream": [{"runtimeClientError": {"message": "boom"}}]}
    ]
    result = svc.invoke("hello")
    final = _wait(svc, result.execution_id)
    assert final["status"] == "failed"
    assert "error" in final


# ---------------------------------------------------------------------------
# Poll semantics
# ---------------------------------------------------------------------------


def test_get_execution_result_unknown_id_raises_not_found() -> None:
    svc = _make_backend()
    with pytest.raises(ChatServiceError) as exc:
        svc.get_execution_result("nope", None)
    assert exc.value.error_kind == "execution_not_found"


def test_get_execution_result_not_configured_raises() -> None:
    svc = HarnessChatBackend(region="us-west-2", harness_arn=None, memory_id=None)
    with pytest.raises(ChatServiceError) as exc:
        svc.get_execution_result("x", None)
    assert exc.value.error_kind == "supervisor_not_configured"


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def test_get_history_parses_events() -> None:
    svc = _make_backend()
    svc._agentcore_client.list_events.return_value = {
        "events": [
            {
                "eventTimestamp": "2026-06-19T01:00:00+00:00",
                "payload": [
                    {"conversational": {"content": {"text": "hi"}, "role": "USER"}},
                    {"conversational": {"content": {"text": "hello"}, "role": "ASSISTANT"}},
                ],
            }
        ]
    }
    msgs = svc.get_history("sess-1")
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert [m.content for m in msgs] == ["hi", "hello"]


def test_get_history_not_configured_raises() -> None:
    svc = _make_backend(memory_id=None)
    with pytest.raises(ChatServiceError) as exc:
        svc.get_history("s")
    assert exc.value.error_kind == "supervisor_not_configured"


# ---------------------------------------------------------------------------
# Route backend selection — harness is the ONLY backend (spec 022 Task 6)
# ---------------------------------------------------------------------------


def test_route_factory_always_selects_harness(monkeypatch) -> None:
    """The route layer must build a harness backend unconditionally.

    Regression guard for spec 022 Task 6: ``AGENT_BACKEND`` used to select the
    backend and **defaulted to ``durable``**, so a missing env var silently
    chose the durable supervisor. That backend no longer exists, so a
    reintroduced selector would route to deleted infrastructure. Setting the
    old flag to its old value must now have no effect.
    """
    import simulator.api.routes_chat as rc

    rc._chat_service = None
    monkeypatch.setenv("HARNESS_ARN", HARNESS_ARN)
    monkeypatch.setenv("AGENTCORE_MEMORY_ID", "mem-1")
    # The retired flag, set to the value that used to select durable.
    monkeypatch.setenv("AGENT_BACKEND", "durable")

    svc = rc._get_chat_service()
    assert isinstance(svc, HarnessChatBackend)
    assert svc.configured is True

    rc._chat_service = None


def test_chat_service_module_exposes_no_durable_service() -> None:
    """``ChatService`` (the durable backend) must stay deleted."""
    import simulator.api.chat_service as cs

    assert not hasattr(cs, "ChatService")


# ---------------------------------------------------------------------------
# Tool-catalog filter (spec 027) — _drive offers select_inline_tools(message)
# ---------------------------------------------------------------------------


def test_drive_offers_full_catalog_when_filter_off(monkeypatch) -> None:
    from simulator.api.harness_tools import INLINE_TOOLS

    monkeypatch.delenv("TOOL_CATALOG_FILTER_ENABLED", raising=False)
    svc = _make_backend()
    svc._agentcore_client.invoke_harness.side_effect = [_text_stream("hi")]
    result = svc.invoke("how were sales today?")
    _wait(svc, result.execution_id)
    tools = svc._agentcore_client.invoke_harness.call_args.kwargs["tools"]
    assert tools is INLINE_TOOLS
    assert len(tools) == 18


def test_drive_offers_filtered_subset_when_enabled(monkeypatch) -> None:
    from simulator.api.harness_tools import INLINE_TOOLS

    monkeypatch.setenv("TOOL_CATALOG_FILTER_ENABLED", "true")
    svc = _make_backend()
    svc._agentcore_client.invoke_harness.side_effect = [_text_stream("hi")]
    result = svc.invoke("how were sales and revenue today?")
    _wait(svc, result.execution_id)
    tools = svc._agentcore_client.invoke_harness.call_args.kwargs["tools"]
    names = {t["name"] for t in tools}
    assert tools is not INLINE_TOOLS
    assert len(tools) < 18
    assert "get_daily_sales" in names and "get_active_alerts" in names  # pos + core
    assert "clock_in" not in names  # schedule excluded


# --- session_id length (2026-08-07) ------------------------------------------


def test_backend_rejects_a_short_session_id_synchronously():
    """Defence-in-depth for non-HTTP callers.

    The route validates first, but a direct caller must not get the old failure
    mode, where botocore raised inside the background thread and the error
    surfaced as a generic "failed" on a later poll.
    """
    from simulator.api.harness_backend import MIN_RUNTIME_SESSION_ID_LENGTH

    backend = HarnessChatBackend(
        region="us-west-2", harness_arn=HARNESS_ARN, memory_id="mem-1",
        iot_endpoint="endpoint-ats.iot.us-west-2.amazonaws.com",
        tool_executor=MagicMock(),
    )
    backend._agentcore_client = MagicMock()

    with pytest.raises(ChatServiceError) as excinfo:
        backend.invoke("hi", "too-short")

    assert excinfo.value.error_kind == "invalid_session_id"
    assert str(MIN_RUNTIME_SESSION_ID_LENGTH) in str(excinfo.value)
    # Nothing was started: no invoke, and no task record to poll.
    backend._agentcore_client.invoke_harness.assert_not_called()
    assert backend._tasks == {}


def test_invalid_session_id_maps_to_400_on_both_endpoints():
    """The error kind must not fall through to the generic 502 default."""
    import simulator.api.routes_chat as rc

    assert rc._http_status_for("invalid_session_id", endpoint="chat") == 400
    assert rc._http_status_for("invalid_session_id", endpoint="result") == 400
