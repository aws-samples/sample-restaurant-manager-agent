# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Correlation-ID dedup cache for idempotent command processing.

Because MQTT QoS 1 guarantees at-least-once delivery, the broker may redeliver
a command the bridge has already processed. The cache holds the response
payload for every recently processed correlation id. On a hit, the bridge
re-publishes the cached payload without re-invoking the tool — ensuring
write operations execute at most once.

The cache is bounded by both size (LRU eviction) and TTL. The TTL should
exceed the maximum expected store-disconnect duration so that commands
redelivered after a prolonged outage are still deduplicated.
"""

from __future__ import annotations

import time
from collections import OrderedDict


class CorrelationIdCache:
    """Bounded LRU + TTL cache mapping correlation id → response payload bytes."""

    def __init__(self, max_size: int, ttl_seconds: int) -> None:
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be >= 1")
        self._max_size = max_size
        self._ttl = float(ttl_seconds)
        # correlation_id → (payload, expires_at_monotonic)
        self._data: OrderedDict[str, tuple[bytes, float]] = OrderedDict()

    def __len__(self) -> int:
        self._purge_expired()
        return len(self._data)

    def __contains__(self, correlation_id: str) -> bool:
        return self.get(correlation_id) is not None

    def get(self, correlation_id: str) -> bytes | None:
        """Return the cached response payload, or ``None`` if miss/expired."""
        entry = self._data.get(correlation_id)
        if entry is None:
            return None
        payload, expires_at = entry
        if time.monotonic() >= expires_at:
            # Expired — treat as a miss and evict.
            self._data.pop(correlation_id, None)
            return None
        # Move to the end for LRU ordering.
        self._data.move_to_end(correlation_id)
        return payload

    def put(self, correlation_id: str, response_payload: bytes) -> None:
        """Insert or refresh a cache entry. Evicts the LRU entry on overflow."""
        now = time.monotonic()
        if correlation_id in self._data:
            self._data.move_to_end(correlation_id)
        self._data[correlation_id] = (response_payload, now + self._ttl)
        # Opportunistic purge so stale entries don't keep the cache full.
        self._purge_expired()
        while len(self._data) > self._max_size:
            self._data.popitem(last=False)

    def _purge_expired(self) -> None:
        now = time.monotonic()
        # Iterate over a snapshot because we mutate the dict.
        for cid, (_, expires_at) in list(self._data.items()):
            if now >= expires_at:
                self._data.pop(cid, None)
