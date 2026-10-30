# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Amazon Nova 2 Sonic voice broker (ADR-005 / spec 025 Phase 1).

Bridges a browser WebSocket <-> a Nova Sonic bidirectional stream. The browser
streams mic PCM (16 kHz, 16-bit, mono) up as binary frames; the broker relays to
Nova Sonic and streams the assistant's audio (24 kHz PCM, binary) and transcripts
(JSON) back. **No AWS credentials reach the browser** — the backend uses its role.

spec 025 Task 1a hardening:
  - **Per-session credentials resolver** (`_BackendCredentialsResolver`) — fresh
    backend-role creds per call, no global `os.environ` mutation (the earlier
    approach), so EC2 instance-role rotation is handled. Validated vs Nova Sonic.
  - **Tool dispatch over the real gateway/IoT round-trip** (`harness_tools.
    execute_tool`, Correlation A) when `IOT_ENDPOINT` is configured (production);
    falls back to the in-process registry for local dev (no live store).

Gated by VOICE_ENABLED. Tool specs + validation come from the
spec-022 parity source (`harness_tools.INLINE_TOOLS` / `validate_params`).
Carry-forward (spec 024): concurrent send/recv with VAD turn-taking; SigV4 needs
service=bedrock + an explicit identity resolver; always answer a toolUse with a
toolResult; 8-minute connection limit (seamless renewal is a follow-on, tied to
session history-seeding, Task 1e).
"""
import asyncio
import base64
import json
import logging
import os
import uuid

from fastapi import WebSocket, WebSocketDisconnect

from simulator.api.history_text import looks_like_record

logger = logging.getLogger(__name__)

REGION = os.environ.get("AWS_REGION", "us-west-2")
MODEL_ID = os.environ.get("SONIC_MODEL_ID", "amazon.nova-2-sonic-v1:0")
SYSTEM_PROMPT = (
    "You are the voice assistant for a restaurant manager. You can call tools to "
    "look up live store data (equipment, orders, inventory, staff) and active alerts. "
    "Keep spoken replies short and conversational — one or two sentences. For questions "
    "about alerts or what needs attention, call get_active_alerts (the proactive alert "
    "inbox); get_equipment_status only reports per-equipment status. For revenue or the "
    "number of orders today, use get_daily_sales (it matches the dashboard's Revenue and "
    "Orders); get_active_orders is only the in-progress queue (the dashboard's Active count)."
)


class _BackendCredentialsResolver:
    """Per-session backend-role credentials (spec 025 R1.1).

    Returns fresh frozen creds from the backend's boto3 default chain on each
    call, so EC2 instance-role rotation is handled and there is **no** global
    ``os.environ`` mutation (the spike's approach). Validated against Nova Sonic.
    """

    def __init__(self):
        import boto3

        self._session = boto3.Session()

    async def get_identity(self, *, properties):
        from smithy_aws_core.identity import AWSCredentialsIdentity

        fc = self._session.get_credentials().get_frozen_credentials()
        return AWSCredentialsIdentity(
            access_key_id=fc.access_key,
            secret_access_key=fc.secret_key,
            session_token=fc.token,
        )


def _make_config():
    """Nova Sonic client config using the backend's own role (no creds to browser)."""
    from aws_sdk_bedrock_runtime.config import Config, HTTPAuthSchemeResolver, SigV4AuthScheme

    sig = SigV4AuthScheme(service="bedrock")
    return Config(
        region=REGION,
        endpoint_uri=f"https://bedrock-runtime.{REGION}.amazonaws.com",
        aws_credentials_identity_resolver=_BackendCredentialsResolver(),
        auth_scheme_resolver=HTTPAuthSchemeResolver(),
        auth_schemes={sig.scheme_id: sig},
    )


def _ev(d):
    return json.dumps(d)


def _compose_alert_text(alert: dict) -> str:
    """Compose a concise proactive-announcement instruction from a self-contained
    spec-023 alert payload (no store round-trip needed for narration)."""
    sev = alert.get("severity", "unknown")
    src = alert.get("source_id") or alert.get("equipment_id") or "equipment"
    cond = alert.get("condition", "an issue")
    reading = alert.get("reading")
    aid = alert.get("alert_id") or alert.get("id") or ""
    parts = [f"severity {sev}", f"equipment {src}", f"condition {cond}"]
    if reading is not None:
        parts.append("reading " + (json.dumps(reading) if isinstance(reading, (dict, list)) else str(reading)))
    detail = "; ".join(parts)
    return (
        "A proactive store alert just fired. Announce it to the restaurant manager out loud in one "
        f"short sentence, then offer to help. Alert details: {detail}; alert_id {aid}. If the manager "
        "asks to acknowledge or dismiss it, call acknowledge_proactive_alert with that alert_id."
    )


# Curated tool set for the real-time voice model. Nova Sonic tool-selection is
# unreliable when offered all 18 store tools (observed on-display: tools past the
# first several -- e.g. get_active_alerts -- were never selected and the model
# said it had "no way" to do them). Offer only the high-value spoken-query tools;
# the gateway/IoT path still exposes every tool to the text harness (R2).
VOICE_TOOL_NAMES = {
    "get_active_alerts",
    "acknowledge_proactive_alert",
    "get_equipment_status",
    "get_daily_sales",
    "get_active_orders",
    "get_stock_levels",
    "get_current_shifts",
}


def _sonic_tools():
    """Build Nova Sonic toolConfiguration tools[] from the REAL store tool specs.

    Reuses harness_tools.INLINE_TOOLS (the spec-022 parity source) so declared
    schemas match the real signatures, filtered to VOICE_TOOL_NAMES so the
    real-time model gets a small, focused set. inputSchema.json is a stringified
    JSON schema.
    """
    from simulator.api.harness_tools import INLINE_TOOLS

    tools = []
    for t in INLINE_TOOLS:
        if t["name"] not in VOICE_TOOL_NAMES:
            continue
        fn = t["config"]["inlineFunction"]
        tools.append({"toolSpec": {
            "name": t["name"],
            "description": fn["description"],
            "inputSchema": {"json": json.dumps(fn["inputSchema"])},
        }})
    return tools


async def _run_tool(name, raw_content, *, registry=None):
    """Validate (parity contract) then dispatch + sanitize.

    Production (``IOT_ENDPOINT`` set): dispatch over the gateway/IoT round-trip
    (Correlation A) via ``harness_tools.execute_tool`` — the same path the text
    harness uses (spec 025 R2.1). Local dev (no ``IOT_ENDPOINT``): fall back to the
    in-process registry so the broker is runnable without a live store.
    """
    from simulator.api.harness_tools import (
        validate_params,
        sanitize_result,
        ToolParamError,
        execute_tool,
    )

    try:
        params = json.loads(raw_content) if raw_content else {}
    except Exception:
        params = {}
    try:
        cleaned = validate_params(name, params)
    except ToolParamError as ex:
        return {"status": "error", "error": {"code": "invalid_params", "message": str(ex)}}
    cleaned.pop("store_id", None)  # store tools don't take store_id (topic routing only)

    iot_endpoint = os.environ.get("IOT_ENDPOINT")
    if iot_endpoint:
        store_id = os.environ.get("STORE_ID", "sim-store-001")
        region = os.environ.get("AWS_REGION", REGION)
        # execute_tool is synchronous (awscrt MQTT round-trip) -> off-load the loop.
        return await asyncio.to_thread(
            execute_tool,
            tool_name=name,
            parameters=cleaned,
            store_id=store_id,
            iot_endpoint=iot_endpoint,
            region=region,
        )
    # Local dev fallback: in-process registry (no IoT round-trip).
    fn = registry.get(name) if registry else None
    if fn is None:
        return {"status": "error", "error": {"code": "unknown_tool", "message": name}}
    try:
        result = await fn(**cleaned)
        return {"status": "success", "result": sanitize_result(result)}
    except Exception as ex:  # noqa: BLE001
        return {"status": "error", "error": {"code": "tool_error", "message": str(ex)}}


class _SonicSession:
    def __init__(self, model_id=MODEL_ID):
        self.model_id = model_id
        self.prompt = str(uuid.uuid4())
        self.sys_c = str(uuid.uuid4())
        self.aud_c = str(uuid.uuid4())
        self.stream = None
        self._send_lock = asyncio.Lock()

    def _history_payloads(self, history):
        """Conversation-history block (Task 1e) for cross-modal continuity.

        Per the Nova Sonic protocol the history goes once, after the system
        prompt and before audio streaming. Each USER/ASSISTANT turn is a TEXT
        content triple; invalid roles / empty content are skipped.

        The block MUST begin with a USER turn -- Nova Sonic rejects the whole
        session otherwise (see ``_drop_leading_assistant_turns``).
        """
        out = []
        for role, content in self._drop_leading_assistant_turns(
            self._seedable_turns(history)
        ):
            hc = str(uuid.uuid4())
            out += [
                _ev({"event": {"contentStart": {"promptName": self.prompt, "contentName": hc, "type": "TEXT",
                    "interactive": False, "role": role, "textInputConfiguration": {"mediaType": "text/plain"}}}}),
                _ev({"event": {"textInput": {"promptName": self.prompt, "contentName": hc, "content": content}}}),
                _ev({"event": {"contentEnd": {"promptName": self.prompt, "contentName": hc}}}),
            ]
        return out

    @staticmethod
    def _seedable_turns(history):
        """Filter history down to ``(ROLE, prose)`` pairs worth seeding."""
        turns = []
        for turn in (history or []):
            role = (turn.get("role") or "").upper()
            if role not in ("USER", "ASSISTANT"):
                continue
            content = turn.get("content")
            if not isinstance(content, str):
                content = turn.get("text") if isinstance(turn.get("text"), str) else ""
            content = content.strip()
            if not content:
                continue
            # Memory can contain messy writes where a turn's "text" is a serialized
            # tool-use/message RECORD ({"message": {...toolUse...}, "message_id": ...})
            # rather than prose. Seeding that makes Nova Sonic vocalize raw tool
            # calls (spec 025 1e on-display bug), so only seed natural-language
            # prose.
            #
            # Voice stays STRICTER than the text-history readers on purpose: those
            # recover prose out of a record, but speaking a mis-extracted fragment
            # aloud is worse than losing a turn of context, so anything
            # record-shaped is skipped outright. Detection is shared with
            # simulator/api/history_text so the marker set cannot drift.
            if looks_like_record(content):
                continue
            turns.append((role, content))
        return turns

    @staticmethod
    def _drop_leading_assistant_turns(turns):
        """Drop turns before the first USER turn.

        Nova Sonic rejects a seeded history whose first message is from the
        assistant, failing the ENTIRE session:

            ValidationException: First message in chat history should not be
            Assistant.

        The session dies on open, so the browser sees "connecting" and drops
        straight back to idle with no audio -- a total voice outage, not a
        degraded turn.

        This is reachable two ways, both data-dependent (which is why it can
        appear long after deploy, with no code change):
          * history genuinely starts with an assistant turn (e.g. a proactive
            alert the display spoke before the manager said anything); or
          * the leading USER turn is dropped by the filters above (empty, or
            record-shaped), promoting an ASSISTANT turn to first.

        Dropping the leading assistant turns loses a little context; seeding
        them loses the whole session.
        """
        for i, (role, _) in enumerate(turns):
            if role == "USER":
                return turns[i:]
        return []

    async def open(self, with_tools=False, history=None):
        from aws_sdk_bedrock_runtime.client import (
            BedrockRuntimeClient,
            InvokeModelWithBidirectionalStreamOperationInput,
        )

        client = BedrockRuntimeClient(config=_make_config())
        self.stream = await asyncio.wait_for(
            client.invoke_model_with_bidirectional_stream(
                InvokeModelWithBidirectionalStreamOperationInput(model_id=self.model_id)),
            timeout=20,
        )
        prompt_start = {"event": {"promptStart": {
            "promptName": self.prompt,
            "textOutputConfiguration": {"mediaType": "text/plain"},
            "audioOutputConfiguration": {"mediaType": "audio/lpcm", "sampleRateHertz": 24000,
                "sampleSizeBits": 16, "channelCount": 1, "voiceId": "matthew", "encoding": "base64", "audioType": "SPEECH"},
        }}}
        if with_tools:
            prompt_start["event"]["promptStart"]["toolUseOutputConfiguration"] = {"mediaType": "application/json"}
            prompt_start["event"]["promptStart"]["toolConfiguration"] = {"tools": _sonic_tools(), "toolChoice": {"auto": {}}}
        payloads = [
            _ev({"event": {"sessionStart": {"inferenceConfiguration": {"maxTokens": 1024, "topP": 0.9, "temperature": 0.7}}}}),
            _ev(prompt_start),
            _ev({"event": {"contentStart": {"promptName": self.prompt, "contentName": self.sys_c, "type": "TEXT",
                "interactive": True, "role": "SYSTEM", "textInputConfiguration": {"mediaType": "text/plain"}}}}),
            _ev({"event": {"textInput": {"promptName": self.prompt, "contentName": self.sys_c, "content": SYSTEM_PROMPT}}}),
            _ev({"event": {"contentEnd": {"promptName": self.prompt, "contentName": self.sys_c}}}),
        ]
        payloads += self._history_payloads(history)  # Task 1e: seed prior turns before audio
        payloads.append(_ev({"event": {"contentStart": {"promptName": self.prompt, "contentName": self.aud_c, "type": "AUDIO",
            "interactive": True, "role": "USER", "audioInputConfiguration": {"mediaType": "audio/lpcm",
            "sampleRateHertz": 16000, "sampleSizeBits": 16, "channelCount": 1, "audioType": "SPEECH", "encoding": "base64"}}}}))
        for payload in payloads:
            await self._send(payload)
            await asyncio.sleep(0.02)

    async def _send(self, payload):
        from aws_sdk_bedrock_runtime.models import (
            InvokeModelWithBidirectionalStreamInputChunk,
            BidirectionalInputPayloadPart,
        )

        async with self._send_lock:
            await self.stream.input_stream.send(InvokeModelWithBidirectionalStreamInputChunk(
                value=BidirectionalInputPayloadPart(bytes_=payload.encode("utf-8"))))

    async def send_audio(self, pcm: bytes):
        b64 = base64.b64encode(pcm).decode("utf-8")
        await self._send(_ev({"event": {"audioInput": {"promptName": self.prompt, "contentName": self.aud_c, "content": b64}}}))

    async def send_tool_result(self, tool_use_id, content_str):
        tc = str(uuid.uuid4())
        await self._send(_ev({"event": {"contentStart": {"promptName": self.prompt, "contentName": tc, "interactive": False,
            "type": "TOOL", "role": "TOOL", "toolResultInputConfiguration": {"toolUseId": tool_use_id, "type": "TEXT",
            "textInputConfiguration": {"mediaType": "text/plain"}}}}}))
        await self._send(_ev({"event": {"toolResult": {"promptName": self.prompt, "contentName": tc, "content": content_str}}}))
        await self._send(_ev({"event": {"contentEnd": {"promptName": self.prompt, "contentName": tc}}}))

    async def speak_alert(self, alert: dict):
        """Proactively SPEAK a spec-023 alert (Task 1c): inject a cross-modal text
        turn instructing the model to announce the self-contained alert payload."""
        cn = str(uuid.uuid4())
        await self._send(_ev({"event": {"contentStart": {"promptName": self.prompt, "contentName": cn,
            "type": "TEXT", "interactive": True, "role": "USER", "textInputConfiguration": {"mediaType": "text/plain"}}}}))
        await self._send(_ev({"event": {"textInput": {"promptName": self.prompt, "contentName": cn,
            "content": _compose_alert_text(alert)}}}))
        await self._send(_ev({"event": {"contentEnd": {"promptName": self.prompt, "contentName": cn}}}))

    async def close(self):
        # Best-effort teardown: the peer may already be gone, so a failure here
        # is not actionable and must not mask the reason the session ended.
        # Logged at debug WITH the exception content, though -- a bare `pass`
        # here hid every teardown fault, and "the session closes cleanly" is
        # indistinguishable from "close() raised on its first await" without it.
        try:
            await self._send(_ev({"event": {"contentEnd": {"promptName": self.prompt, "contentName": self.aud_c}}}))
            await self._send(_ev({"event": {"promptEnd": {"promptName": self.prompt}}}))
            await self._send(_ev({"event": {"sessionEnd": {}}}))
            await self.stream.input_stream.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("voice broker: Nova Sonic session teardown failed: %r", exc)


async def handle_voice_websocket(ws: WebSocket, tool_registry=None):
    """Bridge one browser voice session to Nova Sonic (with optional tool use)."""
    await ws.accept()
    # Fallback-engine seam (spec 024 P6): the browser WS contract (binary PCM up,
    # audio + transcript JSON down) is engine-agnostic. A Transcribe -> harness ->
    # Polly cascade would implement the same contract behind this switch.
    engine = os.environ.get("VOICE_ENGINE", "nova_sonic").lower()
    if engine != "nova_sonic":
        await ws.send_json({"type": "error",
            "message": f"voice engine '{engine}' not implemented (nova_sonic only); the WS contract is engine-agnostic"})
        await ws.close()
        return
    # Optional init handshake (Task 1e): the browser may send {"type":"init",
    # "history":[...]} first to seed cross-modal continuity before audio. A client
    # that sends nothing (or a non-init first frame) is handled by the timeout +
    # the carried-over first message below.
    history = None
    first = None
    try:
        first = await asyncio.wait_for(ws.receive(), timeout=1.0)
    except asyncio.TimeoutError:
        first = None
    if first is not None and first.get("type") == "websocket.disconnect":
        return
    if first is not None and first.get("text"):
        # Same narrowing as the control-frame parse in browser_to_sonic below.
        # A malformed init frame silently degraded cross-modal continuity: the
        # session still came up, just with no seeded history, so the manager got
        # an assistant with amnesia and nothing anywhere said why. Falling back
        # to {} is still right (a bad init must not fail the session), but it is
        # now logged with the offending text.
        try:
            ctrl = json.loads(first["text"])
        except (ValueError, TypeError) as exc:
            logger.warning(
                "voice broker: unparseable init frame, starting with no seeded "
                "history (%r): %.200r",
                exc,
                first["text"],
            )
            ctrl = {}
        if ctrl.get("type") == "init":
            history = ctrl.get("history")
            first = None  # consumed as the init handshake
    sess = _SonicSession()
    try:
        await sess.open(with_tools=tool_registry is not None, history=history)
    except Exception as e:  # noqa: BLE001
        logger.exception("voice broker: Nova Sonic session open failed")
        try:
            await ws.send_json({"type": "error", "message": f"session open failed: {e}"})
            await ws.close()
        except Exception as exc:  # noqa: BLE001
            # The client never learns why the session failed. That is worth a
            # warning rather than silence: the open failure above is already
            # logged, but if this also fails the user sees a dead microphone
            # with no error in the UI, and only this line explains the gap.
            logger.warning(
                "voice broker: could not report session-open failure to client: %r", exc
            )
        return
    await ws.send_json({"type": "ready", "tools": bool(tool_registry)})
    # Replay a non-init first frame (e.g. audio that arrived before any init).
    if first is not None and first.get("bytes"):
        await sess.send_audio(first["bytes"])

    async def browser_to_sonic():
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                return
            data = msg.get("bytes")
            if data:
                await sess.send_audio(data)
                continue
            text = msg.get("text")
            if text:
                # Narrowed from a bare `except Exception` to the parse errors
                # json.loads can actually raise, so a genuine bug in the
                # handling below is no longer swallowed as "bad frame".
                #
                # This is a hot loop, so a silent `continue` here was the
                # dangerous kind of quiet: the control channel carries "stop"
                # and the init frame, and a client sending slightly-wrong JSON
                # would have every control message dropped with the session
                # still looking healthy. Logged at warning with the offending
                # text truncated -- the frames are small control JSON, and
                # without the content the log cannot tell you WHICH frame broke.
                try:
                    ctrl = json.loads(text)
                except (ValueError, TypeError) as exc:
                    logger.warning(
                        "voice broker: ignoring unparseable control frame (%r): %.200r",
                        exc,
                        text,
                    )
                    continue
                if ctrl.get("type") == "stop":
                    return
                if ctrl.get("type") == "speak_alert":
                    await sess.speak_alert(ctrl.get("alert") or {})

    async def sonic_to_browser():
        role = None
        out = await sess.stream.await_output()
        receiver = out[1]
        while True:
            try:
                res = await receiver.receive()
            except StopAsyncIteration:
                return
            if res is None or res.value is None:
                continue
            b = getattr(res.value, "bytes_", None)
            if not b:
                name = type(res.value).__name__
                await ws.send_json({"type": "error", "message": f"{name}: {getattr(res.value, 'message', '')}"})
                return
            e = json.loads(b.decode("utf-8")).get("event", {})
            k = next(iter(e), None)
            if k == "contentStart":
                role = (e["contentStart"].get("role") or "").lower()
            elif k == "textOutput":
                await ws.send_json({"type": "transcript", "role": role or "assistant", "text": e["textOutput"].get("content", "")})
            elif k == "audioOutput":
                await ws.send_bytes(base64.b64decode(e["audioOutput"]["content"]))
            elif k == "userSpeechStart":
                await ws.send_json({"type": "barge_in"})
            elif k == "toolUse":
                tu = e["toolUse"]
                name, tuid, raw = tu.get("toolName"), tu.get("toolUseId"), tu.get("content")
                await ws.send_json({"type": "tool_use", "name": name, "input": raw})
                result = await _run_tool(name, raw, registry=tool_registry)
                await sess.send_tool_result(tuid, json.dumps(result))
                await ws.send_json({"type": "tool_result", "name": name, "ok": result.get("status") != "error"})

    t1 = asyncio.create_task(browser_to_sonic())
    t2 = asyncio.create_task(sonic_to_browser())
    try:
        _done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    except WebSocketDisconnect:
        # Expected: the display navigated away or reloaded. Named narrowly and
        # kept non-fatal, but logged so a disconnect storm is visible.
        logger.debug("voice broker: client disconnected, ending session")
    except Exception:
        logger.exception("voice broker: session error")
    finally:
        await sess.close()
        try:
            await ws.close()
        except Exception as exc:  # noqa: BLE001
            # Already-closed sockets raise here routinely, and this runs in a
            # `finally` -- raising would replace the real error above with a
            # teardown artefact. Debug-logged with content so it is diagnosable.
            logger.debug("voice broker: websocket close failed: %r", exc)
