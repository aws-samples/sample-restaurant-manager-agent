# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""History-text sanitising tests.

Regression guard for the spec-025 1f on-display bug: an assistant tool-use turn
was persisted to AgentCore Memory with its conversational ``content.text`` set to
the serialized message RECORD, so both history readers returned it verbatim — the
text-chat UI rendered raw JSON and Nova Sonic vocalized it aloud.

Fixed on the READ side deliberately: the records were written by the
durable-supervisor path (idle, decommission-bound) and, crucially, bad records are
already stored in Memory and cannot be un-written.
"""
from __future__ import annotations

import json

import pytest

from simulator.api.history_text import (
    RECORD_MARKERS,
    clean_history_text,
    looks_like_record,
)

# The exact shape observed on the display (spec 025 1f).
RAW_TOOL_USE_RECORD = json.dumps(
    {
        "message": {
            "role": "assistant",
            "content": [
                {
                    "toolUse": {
                        "name": "get_equipment_status",
                        "toolUseId": "tooluse_abc123",
                        "input": {},
                    }
                }
            ],
        },
        "message_id": 0,
        "redact_message": None,
        "created_at": "2026-06-28T13:25:50.123456",
        "updated_at": "2026-06-28T13:25:50.123456",
    }
)

# Same wrapper, but the assistant also said something real.
RECORD_WITH_PROSE = json.dumps(
    {
        "message": {
            "role": "assistant",
            "content": [
                {"toolUse": {"name": "get_equipment_status", "toolUseId": "t1", "input": {}}},
                {"text": "The walk-in cooler is at 38°F, within normal range."},
            ],
        },
        "message_id": 3,
        "redact_message": None,
    }
)


class TestLooksLikeRecord:
    def test_prose_is_not_a_record(self):
        assert looks_like_record("The fryer is 12°F above setpoint.") is False

    def test_json_object_is_a_record(self):
        assert looks_like_record(RAW_TOOL_USE_RECORD) is True

    def test_json_array_is_a_record(self):
        assert looks_like_record('[{"toolUse": {}}]') is True

    def test_marker_bearing_text_is_a_record(self):
        assert looks_like_record('oops "message_id" leaked into prose') is True

    def test_empty_and_non_string(self):
        assert looks_like_record("") is False
        assert looks_like_record("   ") is False
        assert looks_like_record(None) is False


class TestCleanHistoryText:
    def test_prose_passes_through_stripped(self):
        assert clean_history_text("  Sales are up 4% today.  ") == "Sales are up 4% today."

    def test_raw_tool_use_record_is_dropped(self):
        """The headline case: never return the raw record."""
        assert clean_history_text(RAW_TOOL_USE_RECORD) is None

    def test_prose_is_recovered_from_a_record(self):
        """Recovery beats discarding when the record still carries real text."""
        assert clean_history_text(RECORD_WITH_PROSE) == (
            "The walk-in cooler is at 38°F, within normal range."
        )

    def test_bare_message_without_wrapper(self):
        text = json.dumps({"role": "assistant", "content": [{"text": "Two fryers need attention."}]})
        assert clean_history_text(text) == "Two fryers need attention."

    def test_tool_result_only_record_is_dropped(self):
        text = json.dumps({"message": {"content": [{"toolResult": {"status": "success"}}]}})
        assert clean_history_text(text) is None

    def test_marker_bearing_but_unparseable_is_dropped(self):
        assert clean_history_text('half a record "toolUse" and then garbage') is None

    def test_empty_and_non_string_are_dropped(self):
        assert clean_history_text("") is None
        assert clean_history_text("   ") is None
        assert clean_history_text(None) is None
        assert clean_history_text({"not": "a string"}) is None

    def test_output_never_contains_record_markers(self):
        """Whatever comes back must be safe to render or speak."""
        for candidate in (RAW_TOOL_USE_RECORD, RECORD_WITH_PROSE, '[{"toolUse":{}}]'):
            out = clean_history_text(candidate)
            if out is not None:
                assert not any(marker in out for marker in RECORD_MARKERS)
                assert out.lstrip()[:1] not in ("{", "[")


# ---------------------------------------------------------------------------
# Both readers must apply the sanitiser (that is the actual bug surface).
# ---------------------------------------------------------------------------


def _events(*texts: str) -> dict:
    """Build a list_events response with one conversational payload per text."""
    return {
        "events": [
            {
                "eventTimestamp": "2026-06-28T13:25:50",
                "payload": [
                    {"conversational": {"role": "assistant", "content": {"text": t}}}
                ],
            }
            for t in texts
        ]
    }


class _FakeAgentCore:
    def __init__(self, response: dict) -> None:
        self._response = response

    def list_events(self, **_kwargs):
        return self._response


@pytest.fixture()
def harness_backend(monkeypatch):
    from simulator.api.harness_backend import HarnessChatBackend

    backend = HarnessChatBackend(region="us-west-2", harness_arn="arn:aws:x", memory_id="mem-1")
    monkeypatch.setattr(
        type(backend),
        "agentcore_client",
        property(lambda self: self._fake),
        raising=False,
    )
    return backend


def test_harness_get_history_drops_raw_records(harness_backend):
    harness_backend._fake = _FakeAgentCore(_events(RAW_TOOL_USE_RECORD, "Sales look healthy."))
    messages = harness_backend.get_history("session-1")
    contents = [m.content for m in messages]
    assert contents == ["Sales look healthy."]
    assert all('"toolUse"' not in c for c in contents)


def test_harness_get_history_recovers_prose_from_a_record(harness_backend):
    harness_backend._fake = _FakeAgentCore(_events(RECORD_WITH_PROSE))
    messages = harness_backend.get_history("session-1")
    assert [m.content for m in messages] == [
        "The walk-in cooler is at 38°F, within normal range."
    ]


def test_harness_get_history_returns_chronological_order(monkeypatch):
    """``list_events`` returns newest-first; history must come back oldest-first.

    Regression guard for the 2026-08-05 voice outage. Reverse-chronological
    history put an ASSISTANT turn first, and Nova Sonic rejects that with
    "First message in chat history should not be Assistant.", failing the whole
    voice session. It also rendered the chat UI backwards.
    """
    from simulator.api.harness_backend import HarnessChatBackend

    # Newest first, exactly as the service returns it.
    newest_first = {
        "events": [
            {
                "eventTimestamp": "2026-08-05T15:12:31.575000+00:00",
                "payload": [
                    {"conversational": {"role": "assistant", "content": {"text": "Inventory looks good."}}}
                ],
            },
            {
                "eventTimestamp": "2026-08-05T15:12:27.321000+00:00",
                "payload": [
                    {"conversational": {"role": "user", "content": {"text": "how is the inventory?"}}}
                ],
            },
        ]
    }

    class _Fake:
        def list_events(self, **kwargs):
            return newest_first

    service = HarnessChatBackend(
        region="us-west-2",
        harness_arn="arn:aws:bedrock-agentcore:us-west-2:1:harness/h-1",
        memory_id="mem-1",
    )
    monkeypatch.setattr(type(service), "agentcore_client", property(lambda self: _Fake()), raising=False)

    msgs = service.get_history("session-1")
    assert [m.role for m in msgs] == ["user", "assistant"], (
        "history must be chronological (user question before assistant reply)"
    )
    assert msgs[0].content == "how is the inventory?"


def test_harness_get_history_drops_raw_records(monkeypatch):
    """History seeding must never surface raw tool-use records as prose.

    Previously covered against the durable ``ChatService``; retargeted to the
    harness backend when the durable layer was removed (spec 022 Task 6). The
    behaviour is the same and still load-bearing: a raw record reaching the
    voice path makes Nova Sonic vocalize JSON (architecture-decisions.md
    § "Amazon Nova Sonic voice edge", item 4).
    """
    from simulator.api.harness_backend import HarnessChatBackend

    service = HarnessChatBackend(
        region="us-west-2",
        harness_arn="arn:aws:bedrock-agentcore:us-west-2:1:harness/h-1",
        memory_id="mem-1",
    )
    monkeypatch.setattr(
        type(service),
        "agentcore_client",
        property(lambda self: _FakeAgentCore(_events(RAW_TOOL_USE_RECORD, "All good."))),
        raising=False,
    )
    contents = [m.content for m in service.get_history("session-1")]
    assert contents == ["All good."]
