# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Regression tests for silently-swallowed exceptions (bandit B110/B112).

WHY
---
A ProtoShield scan flagged five ``except ...: pass`` / ``except ...: continue``
handlers in ``simulator/voice/broker.py`` and ``simulator/api/harness_tools.py``.
They were Low severity, and individually they are all defensible -- teardown
paths and best-effort telemetry genuinely should not raise. The problem was that
they were *silent*, which this project has already paid for twice:

  - a ``ValidationError`` caught-and-logged in the engine tick meant order-reason
    consumption events were never written for the lifetime of every deployment,
    while the service looked healthy;
  - an ``iot-response-handler`` best-effort write logged only
    ``type(exc).__name__``, so the evidence read ``TypeError`` and never
    mentioned that the payload contained floats.

Hence the steering rules these tests encode: best-effort handlers must log the
error's CONTENT (not just its class), and a recurring caught error in a hot loop
should be treated as a likely silent feature outage.

The two behavioural tests below cover the parse handlers, because those are the
ones whose silence had a user-visible cost: a dropped control frame and a voice
session with no seeded history. Both were also narrowed from a bare
``except Exception`` to the errors ``json.loads`` can actually raise -- worth
doing for clarity, though note it is not behaviour-changing here, since each
``try`` body contains only the parse call. What these tests assert is the
logging, which is the part that was missing.

The structural test covers the rest. A teardown handler is awkward to drive from
a test (it needs a half-broken transport), but the invariant that matters is
simply that no handler in these modules is silent -- which is checkable directly
from the AST, and generalises to handlers added later.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
from pathlib import Path

import pytest

from simulator.voice import broker

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Modules whose exception handlers must not be silent. Scoped to the two the
#: scan flagged rather than the whole package, so this is a guard on known-fixed
#: code instead of a repo-wide rule nobody agreed to.
_GUARDED_MODULES = (
    "simulator/voice/broker.py",
    "simulator/api/harness_tools.py",
)


# ---------------------------------------------------------------------------
# Structural: no handler may be silent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel_path", _GUARDED_MODULES)
def test_no_silent_exception_handlers(rel_path: str) -> None:
    """Every ``except`` in these modules must do something observable.

    "Observable" means more than ``pass``/``continue``: swallowing an exception
    with no trace is what turns a broken feature into a healthy-looking service.
    Suppressing the exception is still allowed -- these are best-effort paths --
    it just has to say so.
    """
    source = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
    silent = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ExceptHandler):
            continue
        body = node.body
        if len(body) == 1 and isinstance(body[0], (ast.Pass, ast.Continue)):
            caught = ast.unparse(node.type) if node.type else "bare except"
            silent.append(f"line {node.lineno}: except {caught} -> no logging")

    assert not silent, (
        f"{rel_path} has exception handler(s) that swallow silently:\n  "
        + "\n  ".join(silent)
        + "\n\nBest-effort suppression is fine, but log the exception's content "
        "(e.g. `logger.debug(\"...: %r\", exc)`) so a handler that fires every "
        "time is visible instead of masquerading as success."
    )


# ---------------------------------------------------------------------------
# Behavioural: the two parse handlers log, and stay narrow
# ---------------------------------------------------------------------------


class _FakeReceiver:
    """Sonic output receiver that never yields, so sonic_to_browser parks.

    If it returned instead, ``asyncio.wait(..., FIRST_COMPLETED)`` would race
    the browser task and could cancel it before the frame under test is read.
    """

    async def receive(self):
        await asyncio.sleep(30)


class _FakeStream:
    async def await_output(self):
        return (None, _FakeReceiver())


class _FakeSession:
    """Stands in for _SonicSession: opens cleanly, records what it was given."""

    def __init__(self) -> None:
        self.stream = _FakeStream()
        self.opened_with_history = "not-called"
        self.audio: list[bytes] = []
        self.closed = False

    async def open(self, with_tools=False, history=None):
        self.opened_with_history = history

    async def send_audio(self, pcm: bytes) -> None:
        self.audio.append(pcm)

    async def send_tool_result(self, tool_use_id, content_str) -> None:  # pragma: no cover
        pass

    async def close(self) -> None:
        self.closed = True


class _FakeWebSocket:
    """Feeds a scripted frame sequence, records what was sent back."""

    def __init__(self, frames: list[dict]) -> None:
        self._frames = list(frames)
        self.sent_json: list[dict] = []
        self.accepted = False
        self.closed = False

    async def accept(self) -> None:
        self.accepted = True

    async def receive(self) -> dict:
        if self._frames:
            return self._frames.pop(0)
        # Nothing scripted left: park rather than raising, so a test failure
        # reads as "the expected log never happened" instead of a stray error.
        await asyncio.sleep(30)

    async def send_json(self, payload: dict) -> None:
        self.sent_json.append(payload)

    async def send_bytes(self, data: bytes) -> None:  # pragma: no cover
        pass

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_session(monkeypatch: pytest.MonkeyPatch) -> _FakeSession:
    session = _FakeSession()
    monkeypatch.setattr(broker, "_SonicSession", lambda: session)
    monkeypatch.setenv("VOICE_ENGINE", "nova_sonic")
    return session


async def test_unparseable_control_frame_is_logged_and_skipped(
    fake_session: _FakeSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A malformed control frame must be logged, not silently dropped.

    This handler sits in the browser->Sonic hot loop, and the control channel is
    how the client sends ``stop``. Silently continuing meant a client emitting
    slightly-wrong JSON would have EVERY control message discarded while the
    session still looked healthy -- undiagnosable from the logs.
    """
    ws = _FakeWebSocket(
        [
            {"type": "websocket.receive", "text": json.dumps({"type": "init"})},
            {"type": "websocket.receive", "text": "{not json at all"},
            {"type": "websocket.receive", "text": json.dumps({"type": "stop"})},
        ]
    )

    with caplog.at_level(logging.WARNING, logger=broker.logger.name):
        await asyncio.wait_for(broker.handle_voice_websocket(ws), timeout=10)

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "an unparseable control frame produced no warning"
    message = warnings[0].getMessage()
    assert "control frame" in message
    # The offending payload must be in the log: without it the operator knows
    # only that "a frame" was bad, which does not identify the client bug.
    assert "not json at all" in message

    # And the session must have survived the bad frame to see the stop frame.
    assert fake_session.closed, "session should close after the stop frame"


async def test_unparseable_init_frame_logs_and_seeds_no_history(
    fake_session: _FakeSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A malformed init frame degrades continuity, so it must not be silent.

    Falling back to no history is correct -- a bad init must not fail the
    session -- but this used to happen with no log at all, so the manager got an
    assistant with no memory of the text conversation and nothing explained why.
    """
    ws = _FakeWebSocket(
        [
            {"type": "websocket.receive", "text": '{"type": "init", "history": [BROKEN'},
            {"type": "websocket.receive", "text": json.dumps({"type": "stop"})},
        ]
    )

    with caplog.at_level(logging.WARNING, logger=broker.logger.name):
        await asyncio.wait_for(broker.handle_voice_websocket(ws), timeout=10)

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "an unparseable init frame produced no warning"
    assert "init frame" in warnings[0].getMessage()
    assert fake_session.opened_with_history is None, (
        "a bad init frame must seed no history, not pass through garbage"
    )
