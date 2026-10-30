# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Sanitising of conversational text read back from AgentCore Memory.

Why this exists
---------------
Memory can contain messy writes where an assistant turn's *conversational*
``content.text`` is a serialized message RECORD rather than natural-language
prose, e.g.::

    {"message": {"role": "assistant", "content": [{"toolUse": {"name":
     "get_equipment_status", "toolUseId": "tooluse_...", "input": {}}}]},
     "message_id": 0, "redact_message": null, "created_at": "...", ...}

Both history readers previously returned whatever was stored verbatim, so the
text-chat UI rendered these raw records and (before the spec-025 1e guard) Nova
Sonic *vocalized* them.

Fix placement
-------------
This is read-side hardening, and deliberately so. The bad records were written by
the durable-supervisor path (spec 015), which is idle and slated for
decommission (spec 022 Task 6), and the GA harness does not write conversational
events at all. So a write-side change would patch code that is being deleted --
and, more importantly, it could not help the records **already stored** in
Memory. Sanitising on read fixes history for every consumer, past and future.

Recovery over discarding: if a record still carries real prose in a text block we
return that; only when a turn is purely tool-use/tool-result plumbing (or
unparseable) do we drop it.
"""
from __future__ import annotations

import json
from typing import Any

#: Substrings that mark a string as a serialized message/tool record rather than
#: prose. Mirrored in ``frontend/src/components/VoicePage.jsx::isProseContent``
#: as defence in depth on the client.
RECORD_MARKERS: tuple[str, ...] = (
    '"toolUse"',
    '"toolResult"',
    '"message_id"',
    '"redact_message"',
)


def looks_like_record(text: str) -> bool:
    """True when ``text`` looks like serialized JSON/record content, not prose."""
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if stripped[:1] in ("{", "["):
        return True
    return any(marker in stripped for marker in RECORD_MARKERS)


def _text_blocks(content: Any) -> list[str]:
    """Collect ``text`` values from a Bedrock-style content-block list."""
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    out: list[str] = []
    for block in content:
        if isinstance(block, str):
            out.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            out.append(block["text"])
        # toolUse / toolResult blocks carry no prose -- intentionally skipped.
    return [t for t in (s.strip() for s in out) if t]


def clean_history_text(text: Any) -> str | None:
    """Return display-ready prose for a stored history turn, or ``None`` to skip.

    - prose in, prose out (stripped);
    - a serialized record with a real text block -> that text (recovered);
    - a record that is only tool-use/tool-result plumbing -> ``None``;
    - unparseable or empty -> ``None``.
    """
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped:
        return None

    if not looks_like_record(stripped):
        return stripped

    # Looks like a record: try to recover the prose it may still contain.
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        # Marker-bearing but not valid JSON -- not safe to show, and not
        # recoverable either.
        return None

    candidates: list[Any] = []
    if isinstance(parsed, dict):
        message = parsed.get("message")
        if isinstance(message, dict):
            candidates.append(message.get("content"))
        # A bare message ({"role": ..., "content": [...]}) with no wrapper.
        if "content" in parsed:
            candidates.append(parsed.get("content"))
        if isinstance(parsed.get("text"), str):
            candidates.append(parsed["text"])
    elif isinstance(parsed, list):
        candidates.append(parsed)

    for candidate in candidates:
        blocks = _text_blocks(candidate)
        if blocks:
            return " ".join(blocks)
    return None
