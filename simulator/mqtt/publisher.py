# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Response publisher — publish MQTT responses with disconnect buffering.

When the client is connected, responses go straight to the broker. When the
client is disconnected, responses are held in a bounded FIFO buffer and
flushed (oldest first) when the connection is restored. On buffer overflow,
the oldest buffered response is dropped.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Deque, Tuple

from simulator.mqtt.client import MQTTClient

logger = logging.getLogger(__name__)

_BufferEntry = Tuple[str, bytes]  # (correlation_id or topic, payload)


class ResponsePublisher:
    """Publish per-correlation-id responses with disconnect buffering."""

    def __init__(self, client: MQTTClient, store_id: str, buffer_size: int) -> None:
        self._client = client
        self._store_id = store_id
        self._buffer: Deque[_BufferEntry] = deque(maxlen=buffer_size)

    def topic_for(self, correlation_id: str) -> str:
        return f"stores/{self._store_id}/responses/{correlation_id}"

    def dead_letter_topic(self) -> str:
        return f"stores/{self._store_id}/responses/_invalid"

    async def publish(self, correlation_id: str, payload: bytes) -> None:
        """Publish a response, or buffer it if disconnected."""
        topic = self.topic_for(correlation_id)
        await self._publish_or_buffer(topic, payload, correlation_id)

    async def publish_dead_letter(self, payload: bytes) -> None:
        """Publish to the per-store dead-letter topic for malformed commands."""
        topic = self.dead_letter_topic()
        await self._publish_or_buffer(topic, payload, "_invalid")

    async def publish_event(self, domain: str, payload: bytes) -> None:
        """Publish a store->cloud event (spec 023), e.g. a proactive alert, to
        ``stores/{store_id}/events/{domain}``.

        Reuses the same connected-or-buffer path as responses, so it never
        raises into the caller (fire-and-forget) and survives brief disconnects
        via the outbound buffer.
        """
        topic = f"stores/{self._store_id}/events/{domain}"
        await self._publish_or_buffer(topic, payload, f"event:{domain}")

    async def publish_store_topic(self, suffix: str, payload: bytes) -> None:
        """Publish a store->cloud message to ``stores/{store_id}/{suffix}`` (e.g.
        ``push-subscriptions`` or ``push-requests/{alert_id}`` for Web Push,
        spec 025 Task 1d). Same connected-or-buffer path as events; never raises.
        Deliberately OUTSIDE the ``events/`` prefix so the spec-023 ingest rule
        does not match it."""
        topic = f"stores/{self._store_id}/{suffix}"
        await self._publish_or_buffer(topic, payload, f"store:{suffix}")

    async def _publish_or_buffer(
        self, topic: str, payload: bytes, label: str
    ) -> None:
        # semgrep false positive: `is_connected` is a bool attribute/@property, not a method -- no call intended.
        # nosemgrep: is-function-without-parentheses
        if self._client.is_connected:
            try:
                await self._client.publish(topic, payload, qos=1)
                logger.debug("response_published label=%s bytes=%d", label, len(payload))
                return
            except Exception:  # noqa: BLE001
                logger.exception(
                    "publish failed for label=%s; buffering for retry", label
                )
        self._buffer_append(topic, payload, label)

    def _buffer_append(self, topic: str, payload: bytes, label: str) -> None:
        if self._buffer.maxlen is not None and len(self._buffer) == self._buffer.maxlen:
            dropped = self._buffer.popleft()
            logger.warning(
                "outbound_buffer_overflow dropped_label=%s new_label=%s",
                dropped[0].rsplit("/", 1)[-1],
                label,
            )
        self._buffer.append((topic, payload))
        logger.info(
            "response_buffered label=%s buffer_size=%d", label, len(self._buffer)
        )

    async def on_connect(self, session_present: bool = False) -> None:
        """Flush buffered responses on reconnect."""
        if not self._buffer:
            return
        logger.info(
            "flushing buffered responses count=%d session_present=%s",
            len(self._buffer),
            session_present,
        )
        while self._buffer:
            topic, payload = self._buffer[0]
            try:
                await self._client.publish(topic, payload, qos=1)
                self._buffer.popleft()
                logger.info(
                    "response_flushed label=%s remaining=%d",
                    topic.rsplit("/", 1)[-1],
                    len(self._buffer),
                )
            except Exception:  # noqa: BLE001
                # Keep the entry buffered; retry on next reconnect.
                logger.exception("response flush failed; leaving in buffer")
                return

    @property
    def buffered_count(self) -> int:
        return len(self._buffer)
