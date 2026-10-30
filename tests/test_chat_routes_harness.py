# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""HTTP-level contract tests for the chat endpoints on the harness backend
(spec 022 Task 3, R8).

Drives the real ``routes_chat`` handlers through a FastAPI ``TestClient`` with
the ``HarnessChatBackend`` wired in (agentcore client mocked, tool executor
injected). Confirms the frontend-facing contract shapes are preserved when
``AGENT_BACKEND=harness``.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import simulator.api.routes_chat as rc
from simulator.api.harness_backend import HarnessChatBackend

HARNESS_ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:harness/restaurant_manager_agent-EXAMPLE123"


def _text_stream(text: str) -> dict:
    return {
        "stream": [
            {"contentBlockDelta": {"delta": {"text": text}}},
            {"messageStop": {"stopReason": "end_turn"}},
        ]
    }


@pytest.fixture
def client_and_backend():
    """A TestClient whose chat routes use a harness backend we control."""
    app = FastAPI()
    app.include_router(rc.router, prefix="/api")

    backend = HarnessChatBackend(
        region="us-west-2",
        harness_arn=HARNESS_ARN,
        memory_id="mem-1",
        iot_endpoint="endpoint-ats.iot.us-west-2.amazonaws.com",
        tool_executor=MagicMock(return_value={"status": "success", "result": {}}),
    )
    backend._agentcore_client = MagicMock()
    rc._chat_service = backend
    try:
        yield TestClient(app), backend
    finally:
        rc._chat_service = None


def _poll_until_done(client: TestClient, execution_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/api/chat/result/{execution_id}")
        assert resp.status_code == 200
        body = resp.json()
        if body["status"] != "in_progress":
            return body
        time.sleep(0.02)
    raise AssertionError("result did not reach a terminal state")


# ---------------------------------------------------------------------------
# POST /chat
# ---------------------------------------------------------------------------


def test_post_chat_returns_202_shape(client_and_backend):
    client, backend = client_and_backend
    backend._agentcore_client.invoke_harness.side_effect = [_text_stream("Hi!")]

    resp = client.post("/api/chat", json={"message": "hello"})

    assert resp.status_code == 202
    body = resp.json()
    assert set(body) == {"executionId", "session_id", "status"}
    assert body["status"] == "started"
    assert len(body["session_id"]) >= 33
    assert body["executionId"]


def test_post_chat_empty_message_400(client_and_backend):
    client, _ = client_and_backend
    resp = client.post("/api/chat", json={"message": "   "})
    assert resp.status_code == 400
    assert "error" in resp.json()


# ---------------------------------------------------------------------------
# GET /chat/result/{id}
# ---------------------------------------------------------------------------


def test_result_succeeded_shape(client_and_backend):
    client, backend = client_and_backend
    backend._agentcore_client.invoke_harness.side_effect = [
        _text_stream("All systems normal.")
    ]

    post = client.post("/api/chat", json={"message": "status?"})
    execution_id = post.json()["executionId"]

    body = _poll_until_done(client, execution_id)
    assert body["status"] == "succeeded"
    assert body["response"] == "All systems normal."
    assert set(body) == {"status", "response", "session_id", "timestamp"}


def test_result_unknown_execution_returns_404(client_and_backend):
    client, _ = client_and_backend
    resp = client.get("/api/chat/result/does-not-exist")
    assert resp.status_code == 404
    assert "error" in resp.json()


def test_result_failed_shape(client_and_backend):
    client, backend = client_and_backend
    backend._agentcore_client.invoke_harness.side_effect = [
        {"stream": [{"runtimeClientError": {"message": "boom"}}]}
    ]
    post = client.post("/api/chat", json={"message": "x"})
    body = _poll_until_done(client, post.json()["executionId"])
    assert body["status"] == "failed"
    assert "error" in body


# ---------------------------------------------------------------------------
# GET /chat/history/{session_id}
# ---------------------------------------------------------------------------


def test_history_shape(client_and_backend):
    client, backend = client_and_backend
    backend._agentcore_client.list_events.return_value = {
        "events": [
            {
                "eventTimestamp": "2026-06-19T02:00:00+00:00",
                "payload": [
                    {"conversational": {"content": {"text": "hi"}, "role": "USER"}},
                    {"conversational": {"content": {"text": "hello"}, "role": "ASSISTANT"}},
                ],
            }
        ]
    }
    resp = client.get("/api/chat/history/sess-1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == "sess-1"
    assert body["messages"][0] == {
        "role": "user",
        "content": "hi",
        "timestamp": "2026-06-19T02:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# 503 when the harness backend is not configured
# ---------------------------------------------------------------------------


def test_unconfigured_harness_returns_503():
    app = FastAPI()
    app.include_router(rc.router, prefix="/api")
    rc._chat_service = HarnessChatBackend(
        region="us-west-2", harness_arn=None, memory_id=None
    )
    try:
        client = TestClient(app)
        assert client.post("/api/chat", json={"message": "hi"}).status_code == 503
        assert client.get("/api/chat/result/x").status_code == 503
        assert client.get("/api/chat/history/s").status_code == 503
    finally:
        rc._chat_service = None


# --- session_id length validation (2026-08-07) --------------------------------
#
# AgentCore rejects a `runtimeSessionId` under 33 chars. Before this validation the
# rejection happened inside the background turn loop, so the caller got a generic
# `failed` on a later poll with nothing naming the field.


def test_short_session_id_rejected_with_400(client_and_backend):
    client, _ = client_and_backend
    resp = client.post(
        "/api/chat", json={"message": "hi", "session_id": "task6-verify-1"}
    )
    assert resp.status_code == 400
    error = resp.json()["error"]
    # The message must name the field and the minimum, or it is no better than
    # the generic failure it replaced.
    assert "session_id" in error
    assert str(rc.MIN_RUNTIME_SESSION_ID_LENGTH) in error


def test_short_session_id_rejected_before_any_invoke(client_and_backend):
    """The 400 must arrive instead of a 202, not after one."""
    client, backend = client_and_backend
    resp = client.post("/api/chat", json={"message": "hi", "session_id": "short"})
    assert resp.status_code == 400
    backend._agentcore_client.invoke_harness.assert_not_called()


def test_omitted_session_id_is_accepted(client_and_backend):
    """Omitting it is valid -- the backend generates a compliant one."""
    client, backend = client_and_backend
    backend._agentcore_client.invoke_harness.return_value = _text_stream("ok")
    resp = client.post("/api/chat", json={"message": "hi"})
    assert resp.status_code == 202
    assert len(resp.json()["session_id"]) >= rc.MIN_RUNTIME_SESSION_ID_LENGTH


def test_boundary_length_session_id_is_accepted(client_and_backend):
    client, backend = client_and_backend
    backend._agentcore_client.invoke_harness.return_value = _text_stream("ok")
    exactly_min = "a" * rc.MIN_RUNTIME_SESSION_ID_LENGTH
    resp = client.post("/api/chat", json={"message": "hi", "session_id": exactly_min})
    assert resp.status_code == 202, resp.json()


def test_one_under_the_minimum_is_rejected(client_and_backend):
    """Pins the off-by-one, since the constraint is a `>=`."""
    client, _ = client_and_backend
    resp = client.post(
        "/api/chat",
        json={"message": "hi", "session_id": "a" * (rc.MIN_RUNTIME_SESSION_ID_LENGTH - 1)},
    )
    assert resp.status_code == 400
