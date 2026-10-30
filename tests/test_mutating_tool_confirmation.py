# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Spec 031 / threat-model TM-02: human confirmation before destructive tools.

The property under test is narrow and absolute: **a gated tool must not reach the
store until a human has approved that specific invocation.** Everything else here
exists to make sure that property cannot be satisfied accidentally -- by a replay,
by an expired record, or by the model.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from simulator.api.harness_backend import (
    CONFIRMATION_WINDOW_S,
    ChatServiceError,
    HarnessChatBackend,
)
from simulator.api.harness_tools import (
    CONFIRMATION_REQUIRED_TOOLS,
    KNOWN_TOOLS,
    MUTATING_TOOL_NAMES,
)


class _Store:
    """Records every call that actually reached the store."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {"status": "success", "result": {"voided": True}}


def _backend(store: _Store) -> HarnessChatBackend:
    return HarnessChatBackend(
        harness_arn="arn:aws:bedrock-agentcore:us-west-2:123456789012:harness/x-EXAMPLE123",
        memory_id="Memory-EXAMPLE123",
        store_id="sim-store-001",
        iot_endpoint="example-ats.iot.us-west-2.amazonaws.com",
        region="us-west-2",
        tool_executor=store,
    )


def _gate(backend: HarnessChatBackend, tool="void_order", args='{"order_id": 41, "reason": "spill"}'):
    """Drive one gated tool call and return (execution_id, poll result)."""
    execution_id = "exec-test-1"
    with backend._lock:
        backend._tasks[execution_id] = {
            "state": "in_progress",
            "session_id": "sess-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123",
            "created_at": datetime.now(timezone.utc),
        }
    built = backend._build_tool_result_messages(
        {"id": "tu1", "name": tool, "input": args},
        manager_sub="sub-mgr-1",
        execution_id=execution_id,
    )
    assert built is None, "a gated tool must suspend the turn, not return messages"
    return execution_id, backend.get_execution_result(execution_id, None)


# ---------------------------------------------------------------------------
# The core property
# ---------------------------------------------------------------------------


def test_gated_tool_does_not_reach_the_store_without_approval() -> None:
    store = _Store()
    backend = _backend(store)
    _, result = _gate(backend)
    assert store.calls == [], "void_order executed without human approval"
    assert result["status"] == "confirmation_required"
    assert result["pending"]["tool_name"] == "void_order"
    # The human must see exactly what will run (R1.3).
    assert result["pending"]["parameters"] == {"order_id": 41, "reason": "spill"}
    assert 0 < result["pending"]["expires_in_s"] <= CONFIRMATION_WINDOW_S


def test_approval_executes_the_tool_exactly_once() -> None:
    store = _Store()
    backend = _backend(store)
    _, result = _gate(backend)
    backend.confirm_pending(
        pending_id=result["pending"]["pending_id"], decision="approve"
    )
    for _ in range(100):
        if store.calls:
            break
        time.sleep(0.02)
    assert len(store.calls) == 1, f"expected one store call, got {len(store.calls)}"
    call = store.calls[0]
    assert call["tool_name"] == "void_order"
    assert call["parameters"] == {"order_id": 41, "reason": "spill"}
    assert call["store_id"] == "sim-store-001"


def test_decline_never_calls_the_store() -> None:
    store = _Store()
    backend = _backend(store)
    _, result = _gate(backend)
    backend.confirm_pending(pending_id=result["pending"]["pending_id"], decision="deny")
    time.sleep(0.3)
    assert store.calls == [], "a declined action must not reach the store"


def test_replaying_a_confirmation_is_rejected() -> None:
    """Single-use: a double-click must not void twice (R1.4)."""
    store = _Store()
    backend = _backend(store)
    _, result = _gate(backend)
    pending_id = result["pending"]["pending_id"]
    backend.confirm_pending(pending_id=pending_id, decision="approve")
    with pytest.raises(ChatServiceError) as exc:
        backend.confirm_pending(pending_id=pending_id, decision="approve")
    assert exc.value.error_kind == "pending_not_found"
    for _ in range(100):
        if store.calls:
            break
        time.sleep(0.02)
    assert len(store.calls) == 1, "replay executed the tool a second time"


def test_expired_confirmation_cannot_execute() -> None:
    """R1.5b: a stale client must not be able to complete an abandoned turn."""
    store = _Store()
    backend = _backend(store)
    execution_id, result = _gate(backend)
    with backend._lock:
        backend._tasks[execution_id]["pending"]["expires_at"] = datetime.now(
            timezone.utc
        ) - timedelta(seconds=1)
    with pytest.raises(ChatServiceError) as exc:
        backend.confirm_pending(
            pending_id=result["pending"]["pending_id"], decision="approve"
        )
    assert exc.value.error_kind == "pending_expired"
    time.sleep(0.2)
    assert store.calls == []


def test_expiry_on_poll_reports_that_nothing_changed() -> None:
    """Silent expiry is the worst outcome: the manager must not think it happened."""
    store = _Store()
    backend = _backend(store)
    execution_id, _ = _gate(backend)
    with backend._lock:
        backend._tasks[execution_id]["pending"]["expires_at"] = datetime.now(
            timezone.utc
        ) - timedelta(seconds=1)
    result = backend.get_execution_result(execution_id, None)
    assert result["status"] == "failed"
    assert "nothing was changed" in result["error"]
    assert store.calls == []


def test_bad_decision_value_is_rejected() -> None:
    store = _Store()
    backend = _backend(store)
    _, result = _gate(backend)
    for bad in ("", "yes", "APPROVE", "approve ", None):
        with pytest.raises(ChatServiceError):
            backend.confirm_pending(
                pending_id=result["pending"]["pending_id"], decision=bad
            )
    assert store.calls == []


# ---------------------------------------------------------------------------
# Ungated paths must be untouched (R1.8)
# ---------------------------------------------------------------------------


def test_reads_and_ungated_mutations_are_not_gated() -> None:
    """The friction argument only holds if the other tools really are unaffected."""
    for tool, args in [
        ("get_equipment_status", "{}"),
        ("set_timer", '{"equipment_id": "fryer_1", "minutes": 10}'),
        ("clock_in", '{"employee_id": "e1"}'),
        ("acknowledge_alert", '{"equipment_id": "fryer_1"}'),
    ]:
        store = _Store()
        backend = _backend(store)
        execution_id = "exec-ungated"
        with backend._lock:
            backend._tasks[execution_id] = {
                "state": "in_progress",
                "session_id": "s" * 40,
                "created_at": datetime.now(timezone.utc),
            }
        built = backend._build_tool_result_messages(
            {"id": "tu1", "name": tool, "input": args},
            manager_sub="sub-mgr-1",
            execution_id=execution_id,
        )
        assert built is not None, f"{tool} was gated but should not be"
        assert len(store.calls) == 1, f"{tool} did not execute"


# ---------------------------------------------------------------------------
# Set relationships -- keep the scope decision honest
# ---------------------------------------------------------------------------


def test_gated_set_is_a_subset_of_mutating_and_known() -> None:
    assert CONFIRMATION_REQUIRED_TOOLS <= MUTATING_TOOL_NAMES, (
        "a tool cannot be gated without being classified mutating (R1.7)"
    )
    assert CONFIRMATION_REQUIRED_TOOLS <= KNOWN_TOOLS
    assert CONFIRMATION_REQUIRED_TOOLS == {"void_order", "log_waste"}, (
        "the gated set changed; that is a product decision (spec 031 OD1), so "
        "update the spec rather than the constant"
    )


def test_gated_tools_are_absent_from_the_voice_tool_set() -> None:
    """OD4: voice is out of scope, so no gated tool may be reachable by voice.

    Asserted rather than assumed -- if a gated tool were added to the curated
    voice set, voice would execute it with no confirmation path at all, which is
    strictly worse than the pre-031 behaviour.
    """
    from simulator.voice.broker import VOICE_TOOL_NAMES

    overlap = sorted(CONFIRMATION_REQUIRED_TOOLS & set(VOICE_TOOL_NAMES))
    assert not overlap, (
        f"gated tool(s) {overlap} are offered on the voice path, which has no "
        "confirmation flow (spec 031 OD4). Either remove them from "
        "VOICE_TOOL_NAMES or bring voice into scope."
    )
