# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""WebSocket connection manager for real-time state broadcasting."""

import logging
from fastapi import WebSocket
from simulator.models.simulation import StateEvent
from simulator.state.store import StateStore

logger = logging.getLogger(__name__)


class WebSocketManager:
    """Manages WebSocket connections and broadcasts state events."""

    def __init__(self, state_store: StateStore) -> None:
        self.store = state_store
        self._clients: set[WebSocket] = set()
        state_store.subscribe(None, self.on_state_event)

    @property
    def connection_count(self) -> int:
        """Connected dashboard/display WebSocket clients -- the display
        connectivity signal for Web Push re-engagement (spec 025 Task 1d/1f)."""
        return len(self._clients)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)
        # Send initial full state snapshot
        snapshot = await self.store.get_snapshot()
        await websocket.send_json({"type": "snapshot", "data": snapshot})

    async def disconnect(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)

    async def on_state_event(self, event: StateEvent) -> None:
        """Callback registered with StateStore — broadcasts to all clients."""
        msg = {"type": "event", "domain": event.domain, "mutation_type": event.mutation_type, "data": event.data}
        failed = []
        for ws in list(self._clients):
            try:
                await ws.send_json(msg)
            except Exception as e:
                logger.debug("WebSocket send failed: %s", e)
                failed.append(ws)
        for ws in failed:
            self._clients.discard(ws)

    async def broadcast_agent_log(self, source: str, fields: dict) -> None:
        """Forward a structured log event (e.g. MQTT bridge activity) to clients.

        Emitted as ``{type: "api_call", source, ...}`` so the dashboard's agent
        log view can surface REST, MCP, and MQTT events together.
        """
        msg = {"type": "api_call", "source": source, **fields}
        failed = []
        for ws in list(self._clients):
            try:
                await ws.send_json(msg)
            except Exception as e:
                logger.debug("WebSocket send failed: %s", e)
                failed.append(ws)
        for ws in failed:
            self._clients.discard(ws)
