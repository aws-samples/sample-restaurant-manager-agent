# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Command dispatcher — the brain of the MQTT bridge.

The dispatcher is the ``on_message`` callback registered with the MQTT client.
For every inbound message it:

1. Parses the JSON payload. Malformed JSON or missing required fields go to
   the per-store dead-letter topic. No tool is invoked.
2. Checks the correlation-id cache. On a hit, re-publishes the cached
   response without invoking the tool (idempotency for redelivered commands).
3. Validates that the command-topic domain segment matches the tool's
   expected domain. Mismatches return a structured error.
4. Looks up the tool in the MCP Tool Registry (spec 001). Unknown tool names
   return a structured error.
5. Dispatches the tool inside a bounded concurrency semaphore. Timeouts,
   exceptions, and tool-level error envelopes all get wrapped into the
   structured response shape. The response is cached and handed to the
   :class:`ResponsePublisher`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from simulator.mcp.registry import DOMAIN_OF_TOOL
from simulator.mqtt.dedup import CorrelationIdCache
from simulator.mqtt.publisher import ResponsePublisher

logger = logging.getLogger(__name__)

ToolFunc = Callable[..., Awaitable[dict]]

_VALID_DOMAINS = frozenset(set(DOMAIN_OF_TOOL.values()))


class CommandDispatcher:
    """Route inbound MQTT command messages to local MCP tools."""

    def __init__(
        self,
        tool_registry: dict[str, ToolFunc],
        dedup: CorrelationIdCache,
        publisher: ResponsePublisher,
        store_id: str,
        concurrency_limit: int = 8,
        inbound_queue_size: int = 64,
        sim_time_fn: Callable[[], str] | None = None,
    ) -> None:
        self._registry = tool_registry
        self._dedup = dedup
        self._publisher = publisher
        self._store_id = store_id
        self._sim_time_fn = sim_time_fn
        self._semaphore = asyncio.Semaphore(concurrency_limit)
        self._inflight = 0
        self._inflight_lock = asyncio.Lock()
        self._inbound_queue_size = inbound_queue_size
        self._concurrency_limit = concurrency_limit
        # Structured-log observer hook: each event dict is passed to the
        # callable if set. Used by main.py to surface events on the WebSocket
        # agent-log stream.
        self._log_observer: Callable[[dict], Any] | None = None
        # Counters for operational visibility.
        self.counters = {
            "received": 0,
            "published": 0,
            "dedup_hit": 0,
            "malformed": 0,
            "unknown_tool": 0,
            "topic_domain_mismatch": 0,
            "overloaded": 0,
            "internal_error": 0,
        }

    def set_log_observer(self, observer: Callable[[dict], Any] | None) -> None:
        self._log_observer = observer

    # --- Entry point ---

    async def on_message(self, topic: str, payload: bytes) -> None:
        """Entry point registered with :class:`MQTTClient`."""
        self.counters["received"] += 1
        try:
            command = json.loads(payload.decode("utf-8"))
            if not isinstance(command, dict):
                raise ValueError("payload is not a JSON object")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as e:
            await self._send_dead_letter(
                payload, f"malformed_json: {e.__class__.__name__}: {e}"
            )
            return

        correlation_id = command.get("correlation_id")
        tool_name = command.get("tool_name")

        if not isinstance(correlation_id, str) or not correlation_id:
            await self._send_dead_letter(payload, "missing_or_invalid_correlation_id")
            return
        if not isinstance(tool_name, str) or not tool_name:
            await self._send_dead_letter(payload, "missing_or_invalid_tool_name")
            return

        self._log_event(
            "command_received",
            correlation_id=correlation_id,
            tool_name=tool_name,
            param_bytes=len(json.dumps(command.get("parameters", {}))),
        )

        # Dedup replay.
        cached = self._dedup.get(correlation_id)
        if cached is not None:
            self.counters["dedup_hit"] += 1
            self._log_event(
                "command_dedup_hit",
                correlation_id=correlation_id,
                tool_name=tool_name,
            )
            await self._publisher.publish(correlation_id, cached)
            self.counters["published"] += 1
            return

        # Topic-domain validation.
        expected_domain = DOMAIN_OF_TOOL.get(tool_name)
        if expected_domain is None:
            self.counters["unknown_tool"] += 1
            await self._respond_error(
                correlation_id,
                tool_name,
                code="unknown_tool",
                message=f"Unknown tool: {tool_name!r}",
                start=time.monotonic(),
            )
            return

        topic_domain = topic.rsplit("/", 1)[-1]
        if topic_domain != expected_domain:
            self.counters["topic_domain_mismatch"] += 1
            await self._respond_error(
                correlation_id,
                tool_name,
                code="topic_domain_mismatch",
                message=(
                    f"Tool {tool_name!r} expected on domain {expected_domain!r} "
                    f"but command arrived on {topic_domain!r}"
                ),
                start=time.monotonic(),
            )
            return

        # Backpressure: reject new commands when the semaphore+inflight exceeds
        # the inbound queue size.
        async with self._inflight_lock:
            if self._inflight >= self._inbound_queue_size:
                self.counters["overloaded"] += 1
                await self._respond_error(
                    correlation_id,
                    tool_name,
                    code="overloaded",
                    message="Store is overloaded; retry later.",
                    start=time.monotonic(),
                )
                return
            self._inflight += 1

        try:
            await self._invoke(correlation_id, tool_name, command.get("parameters", {}))
        finally:
            async with self._inflight_lock:
                self._inflight -= 1

    # --- Tool invocation ---

    async def _invoke(
        self, correlation_id: str, tool_name: str, parameters: Any
    ) -> None:
        if not isinstance(parameters, dict):
            parameters = {}
        # Strip gateway-level parameters that tools don't expect.
        # The gateway Lambda uses store_id for MQTT topic routing and
        # the Strands SDK may inject a 'kwargs' key from **kwargs signatures.
        parameters.pop("store_id", None)
        parameters.pop("kwargs", None)
        tool = self._registry.get(tool_name)
        if tool is None:
            # Shouldn't happen since DOMAIN_OF_TOOL is the same source of truth,
            # but defend against drift.
            self.counters["unknown_tool"] += 1
            await self._respond_error(
                correlation_id,
                tool_name,
                code="unknown_tool",
                message=f"Tool {tool_name!r} not registered",
                start=time.monotonic(),
            )
            return

        start = time.monotonic()
        async with self._semaphore:
            try:
                result = await tool(**parameters)
            except TypeError as e:
                # Bad arg signature — treat as invalid input.
                await self._respond_error(
                    correlation_id,
                    tool_name,
                    code="invalid_input",
                    message=f"Invalid parameters for {tool_name}: {e}",
                    start=start,
                )
                return
            except Exception as e:  # noqa: BLE001
                self.counters["internal_error"] += 1
                logger.exception(
                    "tool invocation raised correlation_id=%s tool=%s",
                    correlation_id,
                    tool_name,
                )
                await self._respond_error(
                    correlation_id,
                    tool_name,
                    code="internal_error",
                    message=str(e) or e.__class__.__name__,
                    start=start,
                )
                return

        duration_ms = int((time.monotonic() - start) * 1000)
        if isinstance(result, dict) and "error" in result:
            # Tool returned a structured error envelope.
            await self._respond_error(
                correlation_id,
                tool_name,
                code=str(result.get("code", "invalid_input")),
                message=str(result["error"]),
                start=None,
                duration_ms=duration_ms,
            )
            return
        await self._respond_success(
            correlation_id, tool_name, result, duration_ms=duration_ms
        )

    # --- Response builders ---

    async def _respond_success(
        self,
        correlation_id: str,
        tool_name: str,
        result: Any,
        duration_ms: int,
    ) -> None:
        response = {
            "correlation_id": correlation_id,
            "tool_name": tool_name,
            "status": "success",
            "result": result,
            "duration_ms": duration_ms,
            "timestamp": _now_iso(),
        }
        if self._sim_time_fn is not None:
            response["sim_time"] = self._sim_time_fn()
        payload = _encode_json(response)
        self._dedup.put(correlation_id, payload)
        await self._publisher.publish(correlation_id, payload)
        self.counters["published"] += 1
        self._log_event(
            "response_published",
            correlation_id=correlation_id,
            tool_name=tool_name,
            status="success",
            duration_ms=duration_ms,
        )

    async def _respond_error(
        self,
        correlation_id: str,
        tool_name: str,
        code: str,
        message: str,
        start: float | None = None,
        duration_ms: int | None = None,
    ) -> None:
        if duration_ms is None:
            duration_ms = 0 if start is None else int((time.monotonic() - start) * 1000)
        payload = _encode_json(
            {
                "correlation_id": correlation_id,
                "tool_name": tool_name,
                "status": "error",
                "error": {"code": code, "message": message},
                "duration_ms": duration_ms,
                "timestamp": _now_iso(),
            }
        )
        self._dedup.put(correlation_id, payload)
        await self._publisher.publish(correlation_id, payload)
        self.counters["published"] += 1
        self._log_event(
            "response_published",
            correlation_id=correlation_id,
            tool_name=tool_name,
            status="error",
            code=code,
            duration_ms=duration_ms,
        )

    async def _send_dead_letter(self, original_payload: bytes, message: str) -> None:
        self.counters["malformed"] += 1
        b64 = base64.b64encode(original_payload).decode("ascii")
        payload = _encode_json(
            {
                "error": {"code": "malformed_command", "message": message},
                "original_payload_base64": b64,
                "timestamp": _now_iso(),
            }
        )
        await self._publisher.publish_dead_letter(payload)
        self._log_event("command_dead_lettered", message=message)

    # --- Structured logging ---

    def _log_event(self, event: str, **fields: Any) -> None:
        fields["event"] = event
        logger.info("%s", fields)
        observer = self._log_observer
        if observer is not None:
            try:
                maybe = observer(fields)
                if asyncio.iscoroutine(maybe):
                    asyncio.create_task(maybe)
            except Exception:  # noqa: BLE001
                logger.exception("log observer failed")


def _encode_json(obj: Any) -> bytes:
    return json.dumps(obj, default=_default_encoder, separators=(",", ":")).encode("utf-8")


def _default_encoder(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
