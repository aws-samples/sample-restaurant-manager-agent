# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Main entrypoint — starts simulation engine, REST API, WebSocket server, and MQTT bridge."""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import WebSocket, WebSocketDisconnect

from simulator.api.app import create_app
from simulator.engine.clock import VirtualClock
from simulator.engine.engine import SimulationEngine
from simulator.mcp.registry import build_tool_registry
from simulator.models.simulation import SimulationConfig
from simulator.mqtt.bridge import MQTTBridge
from simulator.mqtt.config import MQTTConnectionConfig
from simulator.state.store import StateStore
from simulator.websocket.server import WebSocketManager

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CONFIG_DIR = Path("config")


def load_config() -> SimulationConfig:
    """Load and validate simulation config."""
    try:
        data = json.loads((CONFIG_DIR / "simulation.json").read_text())
        return SimulationConfig(**data)
    except Exception as e:
        logger.error("Failed to load simulation config: %s", e)
        sys.exit(1)


async def main() -> None:
    config = load_config()
    start_time = datetime.now().replace(hour=7, minute=0, second=0, microsecond=0)

    clock = VirtualClock(
        start_time=start_time,
        speed_multiplier=config.default_speed_multiplier,
        tick_interval_seconds=config.tick_interval_seconds,
    )
    store = StateStore(clock_state=clock.get_state(), config=config)
    engine = SimulationEngine(clock, store, config, config_dir=str(CONFIG_DIR))

    try:
        await engine.initialize()
    except Exception as e:
        logger.error("Engine initialization failed: %s", e)
        sys.exit(1)

    # Build the MCP tool registry (spec 001). The MQTT bridge (spec 002)
    # dispatches inbound commands through this registry; it is also exposed
    # on the FastAPI app state for future tooling.
    tool_registry = build_tool_registry(store)
    logger.info("Tool registry built with %d tools", len(tool_registry))

    ws_manager = WebSocketManager(store)

    # MQTT bridge (spec 002) — optional. Activated by MQTT_ENABLED=true.
    bridge: MQTTBridge | None = None
    mqtt_cfg = MQTTConnectionConfig.load_from_env()
    if mqtt_cfg.enabled:
        async def _handle_notification(notification: dict) -> None:
            # Cloud-approved proactive alert -> inbox; Web Push re-engagement
            # fires only when no display is connected (spec 023 Task 6 + 025 1d/1f).
            await store.handle_incoming_notification(
                notification,
                sim_time=clock.current_time,
                display_connected=ws_manager.connection_count > 0,
            )

        bridge = MQTTBridge(mqtt_cfg, tool_registry,
                           sim_time_fn=lambda: clock.current_time.isoformat(),
                           notification_handler=_handle_notification)

        async def _forward_mqtt_event(fields: dict) -> None:
            await ws_manager.broadcast_agent_log("mqtt", fields)

        bridge.set_log_observer(_forward_mqtt_event)
        try:
            await bridge.start()
            logger.info(
                "MQTT bridge connected (store_id=%s endpoint=%s)",
                mqtt_cfg.store_id,
                mqtt_cfg.broker_endpoint,
            )
            # Wire proactive alert-event publishing (spec 023 Task 3): the
            # equipment engine emits store->cloud alert events via the bridge.
            if bridge.publisher is not None and engine.equipment is not None:
                from simulator.mqtt.events import AlertEventPublisher

                _alert_pub = AlertEventPublisher(bridge.publisher, mqtt_cfg.store_id)
                engine.equipment.alert_sink = _alert_pub
                if engine.inventory is not None:
                    engine.inventory.alert_sink = _alert_pub  # spec 023 ext: inventory low-stock
                # Let the proactive-ack path (REST + the agent's
                # acknowledge_proactive_alert tool) publish events/ack through the
                # same sink so the cloud workflow resumes (spec 023 Task 11).
                store.alert_ack_publisher = _alert_pub.publish_ack
                # Web Push relay + re-engagement requests (spec 025 Task 1d): the
                # store republishes browser subscriptions and (when no display is
                # connected) re-engagement requests to the cloud over IoT.
                import json as _json

                _push_pub = bridge.publisher
                _sid = mqtt_cfg.store_id

                async def _relay_push_subscription(subscription):
                    await _push_pub.publish_store_topic(
                        "push-subscriptions",
                        _json.dumps({"store_id": _sid, "subscription": subscription}).encode("utf-8"),
                    )

                async def _request_push(alert_id, notification):
                    await _push_pub.publish_store_topic(
                        f"push-requests/{alert_id}",
                        _json.dumps(
                            {"store_id": _sid, "alert_id": alert_id, "notification": notification}
                        ).encode("utf-8"),
                    )

                store.push_subscription_publisher = _relay_push_subscription
                store.push_request_publisher = _request_push
                logger.info("Alert-event + Web Push relay wired to the MQTT bridge")
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "MQTT bridge failed to start: %s (continuing without MQTT)",
                e,
            )
            bridge = None
    else:
        logger.info("MQTT bridge disabled (set MQTT_ENABLED=true to activate)")

    app = create_app(store, engine, clock)

    # semgrep's useless-inner-function does not model decorator side effects:
    # the decorator registers the route, so nothing calls this by name.
    # nosemgrep: useless-inner-function
    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await ws_manager.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            await ws_manager.disconnect(websocket)

    # Voice edge (ADR-005 / spec 025): flag-gated Nova Sonic voice broker. This is
    # the permanent Phase-1 voice implementation, on-display validated; the flag
    # exists so a deployment can run without voice, not because it is throwaway.
    if os.environ.get("VOICE_ENABLED", "").lower() in ("1", "true", "yes"):
        from simulator.voice.broker import handle_voice_websocket

        # nosemgrep: useless-inner-function
        @app.websocket("/ws/voice")
        async def voice_endpoint(websocket: WebSocket):
            await handle_voice_websocket(websocket, tool_registry=tool_registry)

        logger.info("Voice edge ENABLED: ws /ws/voice")
    else:
        logger.info("Voice edge disabled (set VOICE_ENABLED=true)")

    await clock.start()
    logger.info("Simulation started at %s (speed: %sx)", start_time, config.default_speed_multiplier)

    # Loopback by default so a local run is not reachable from the network. The
    # EC2 unit (restaurant-sim.service) sets SIM_BIND_HOST to all interfaces so
    # the ALB can reach :8000; its security group admits only the ALB.
    host = os.environ.get("SIM_BIND_HOST", "127.0.0.1")
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=8000, log_level="info"))
    try:
        await server.serve()
    finally:
        if bridge is not None:
            await bridge.stop()


if __name__ == "__main__":
    asyncio.run(main())
