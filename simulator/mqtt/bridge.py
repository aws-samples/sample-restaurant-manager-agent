# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""MQTT bridge orchestration.

Wires the configuration, dedup cache, publisher, dispatcher, and MQTT client
together. ``main.py`` constructs one :class:`MQTTBridge` when MQTT is enabled
and calls :meth:`start` after the MCP Tool Registry is built. On shutdown,
:meth:`stop` disconnects cleanly within the configured timeout.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

from simulator.mqtt.client import MQTTClient
from simulator.mqtt.config import MQTTConnectionConfig
from simulator.mqtt.dedup import CorrelationIdCache
from simulator.mqtt.dispatcher import CommandDispatcher, ToolFunc
from simulator.mqtt.publisher import ResponsePublisher

logger = logging.getLogger(__name__)


class MQTTBridge:
    """Top-level orchestrator for the store-side MQTT bridge."""

    def __init__(
        self,
        cfg: MQTTConnectionConfig,
        tool_registry: dict[str, ToolFunc],
        sim_time_fn: Callable[[], str] | None = None,
        notification_handler: Callable[[dict], Any] | None = None,
    ) -> None:
        self._cfg = cfg
        self._tool_registry = tool_registry
        self._sim_time_fn = sim_time_fn
        self._notification_handler = notification_handler
        self._dedup: CorrelationIdCache | None = None
        self._publisher: ResponsePublisher | None = None
        self._dispatcher: CommandDispatcher | None = None
        self._client: MQTTClient | None = None
        self._log_observer: Callable[[dict], Any] | None = None

    def set_log_observer(self, observer: Callable[[dict], Any] | None) -> None:
        """Observe dispatcher structured-log events (used to feed WebSocket)."""
        self._log_observer = observer
        if self._dispatcher is not None:
            self._dispatcher.set_log_observer(observer)

    @property
    def is_connected(self) -> bool:
        # semgrep false positive: `is_connected` is a bool attribute/@property, not a method -- no call intended.
        # nosemgrep: is-function-without-parentheses
        return self._client.is_connected if self._client else False

    @property
    def counters(self) -> dict[str, int]:
        return dict(self._dispatcher.counters) if self._dispatcher else {}

    @property
    def publisher(self) -> ResponsePublisher | None:
        """The response/event publisher (None until :meth:`start`). Used to wire
        the proactive alert-event sink (spec 023 Task 3)."""
        return self._publisher

    async def start(self) -> None:
        """Construct components, connect to the broker, and subscribe."""
        if not self._cfg.enabled:
            logger.info("MQTT bridge is disabled; start() is a no-op")
            return

        self._dedup = CorrelationIdCache(
            max_size=self._cfg.dedup_cache_size,
            ttl_seconds=self._cfg.dedup_cache_ttl_seconds,
        )
        # Client built first so publisher can hold a reference. The client's
        # on_connect delegates to publisher.on_connect to flush the buffer.
        self._client = MQTTClient(
            cfg=self._cfg,
            on_message=self._on_message_placeholder,
        )
        self._publisher = ResponsePublisher(
            self._client,
            store_id=self._cfg.store_id,
            buffer_size=self._cfg.outbound_buffer_size,
        )
        self._dispatcher = CommandDispatcher(
            tool_registry=self._tool_registry,
            dedup=self._dedup,
            publisher=self._publisher,
            store_id=self._cfg.store_id,
            concurrency_limit=self._cfg.concurrent_tool_limit,
            inbound_queue_size=self._cfg.inbound_queue_size,
            sim_time_fn=self._sim_time_fn,
        )
        if self._log_observer is not None:
            self._dispatcher.set_log_observer(self._log_observer)

        # Route inbound messages: commands -> dispatcher, notifications -> inbox.
        self._client._on_message = self._route_message  # type: ignore[attr-defined]
        self._client._on_connect = self._publisher.on_connect  # type: ignore[attr-defined]

        await self._client.connect()
        await self._client.subscribe(self._cfg.topic_commands_wildcard(), qos=1)
        if self._notification_handler is not None:
            await self._client.subscribe(self._cfg.topic_notifications_wildcard(), qos=1)
        logger.info(
            "mqtt bridge started store_id=%s commands_topic=%s notifications=%s",
            self._cfg.store_id,
            self._cfg.topic_commands_wildcard(),
            self._notification_handler is not None,
        )

    async def stop(self) -> None:
        """Disconnect from the broker within the configured shutdown timeout."""
        if self._client is None:
            return
        logger.info(
            "mqtt bridge stopping; counters=%s buffered=%d",
            self.counters,
            self._publisher.buffered_count if self._publisher else 0,
        )
        try:
            await asyncio.wait_for(
                self._client.disconnect(),
                timeout=self._cfg.shutdown_timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "mqtt disconnect exceeded %.1fs; forcing shutdown",
                self._cfg.shutdown_timeout_seconds,
            )

    async def _route_message(self, topic: str, payload: bytes) -> None:
        """Route inbound MQTT messages by topic.

        Cloud->store proactive notifications go to the inbox handler; everything
        else is a command for the dispatcher (spec 023 Task 6).
        """
        if "/notifications/" in topic and self._notification_handler is not None:
            try:
                notification = json.loads(payload)
            except (ValueError, TypeError):
                logger.warning("dropping non-JSON notification on topic=%s", topic)
                return
            try:
                await self._notification_handler(notification)
            except Exception:  # noqa: BLE001 - delivery must not break the message pump
                logger.exception("notification handler raised for topic=%s", topic)
            return
        await self._dispatcher.on_message(topic, payload)

    async def _on_message_placeholder(self, topic: str, payload: bytes) -> None:
        # Swapped out in start() once dispatcher is constructed.
        logger.warning(
            "mqtt bridge received message before dispatcher was wired (topic=%s)",
            topic,
        )
