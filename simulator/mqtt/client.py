# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""MQTT client wrapper around aiomqtt.

Responsibilities:

* Establish an outbound TLS connection using the configured auth mode
  (``userpass`` for Amazon MQ / ActiveMQ, ``mtls`` for AWS IoT Core).
* Maintain a persistent session so the broker retains QoS 1 messages during
  brief disconnects (``clean_session=False``, stable client id).
* Run a supervised reconnect loop with exponential backoff + jitter.
* Deliver inbound messages to an injected ``on_message`` async callback.
* Re-subscribe on reconnect only when the broker reports ``session_present=false``.

The wrapper exposes a narrow interface (``connect``, ``disconnect``,
``publish``, ``subscribe``, ``is_connected``) so downstream components
(dispatcher, publisher) never see the underlying library.
"""

from __future__ import annotations

import asyncio
import logging
import random
import ssl
from typing import Awaitable, Callable

import aiomqtt

from simulator.mqtt.config import MQTTConnectionConfig

logger = logging.getLogger(__name__)

MessageHandler = Callable[[str, bytes], Awaitable[None]]
ConnectHandler = Callable[[bool], Awaitable[None]]  # session_present

_BACKOFF_BASE = 1.0
_BACKOFF_CAP = 60.0
_JITTER_FRAC = 0.25


class MQTTClient:
    """Thin async wrapper around aiomqtt with reconnect supervision."""

    def __init__(
        self,
        cfg: MQTTConnectionConfig,
        on_message: MessageHandler,
        on_connect: ConnectHandler | None = None,
    ) -> None:
        self._cfg = cfg
        self._on_message = on_message
        self._on_connect = on_connect
        self._client: aiomqtt.Client | None = None
        self._client_lock = asyncio.Lock()
        self._connected = asyncio.Event()
        self._stop = asyncio.Event()
        self._supervisor: asyncio.Task | None = None
        self._message_pump: asyncio.Task | None = None
        self._subscriptions: list[tuple[str, int]] = []

    # --- Public API ---

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    async def connect(self) -> None:
        """Start the supervised connection loop.

        Returns once the first connection succeeds. If the initial connect
        fails repeatedly, this coroutine keeps waiting — the caller is
        expected to apply its own startup timeout if desired.
        """
        if self._supervisor is not None:
            return
        self._stop.clear()
        self._supervisor = asyncio.create_task(
            self._run_supervisor(), name="mqtt-supervisor"
        )
        await self._connected.wait()

    async def disconnect(self) -> None:
        """Stop the supervisor and close the connection gracefully."""
        self._stop.set()
        # Nudge any waiters.
        self._connected.clear()
        if self._message_pump:
            self._message_pump.cancel()
        if self._supervisor:
            self._supervisor.cancel()
            try:
                await self._supervisor
            except asyncio.CancelledError:
                pass
        self._supervisor = None
        self._message_pump = None
        self._client = None

    async def subscribe(self, topic_filter: str, qos: int = 1) -> None:
        """Subscribe, and remember the subscription for reconnect."""
        self._subscriptions.append((topic_filter, qos))
        client = await self._require_client()
        await client.subscribe(topic_filter, qos=qos)
        logger.info("mqtt subscribed topic=%s qos=%d", topic_filter, qos)

    async def publish(self, topic: str, payload: bytes, qos: int = 1) -> None:
        """Publish to the broker. Raises if not connected."""
        client = await self._require_client()
        await client.publish(topic, payload=payload, qos=qos)

    # --- Internals ---

    async def _require_client(self) -> aiomqtt.Client:
        if self._client is None or not self._connected.is_set():
            raise RuntimeError("MQTT client is not connected")
        return self._client

    def _build_tls_params(self) -> aiomqtt.TLSParameters:
        cfg = self._cfg
        kwargs = {
            "tls_version": ssl.PROTOCOL_TLS_CLIENT,
        }
        if cfg.ca_bundle_path:
            kwargs["ca_certs"] = cfg.ca_bundle_path
        if cfg.auth_mode == "mtls":
            kwargs["certfile"] = cfg.client_cert_path
            kwargs["keyfile"] = cfg.client_key_path
        return aiomqtt.TLSParameters(**kwargs)

    def _build_ssl_context(self, protocols: list[str]) -> ssl.SSLContext:
        """Build an SSLContext that negotiates MQTT via ALPN (the 443 path).

        ``aiomqtt.TLSParameters`` (which maps to paho's ``tls_set``) cannot set
        the ALPN extension, so the ALPN path builds its own context and passes
        it via ``aiomqtt.Client(tls_context=...)``. The context verifies the
        broker certificate and hostname (server auth), loads the client
        certificate for mTLS, and advertises the ALPN protocol the broker uses
        to select MQTT (``x-amzn-mqtt-ca`` for AWS IoT Core X.509 client-cert
        connections on port 443).
        """
        cfg = self._cfg
        ctx = ssl.create_default_context(
            ssl.Purpose.SERVER_AUTH,
            cafile=cfg.ca_bundle_path or None,
        )
        if cfg.auth_mode == "mtls":
            ctx.load_cert_chain(
                certfile=cfg.client_cert_path,
                keyfile=cfg.client_key_path,
            )
        ctx.set_alpn_protocols(protocols)
        return ctx

    def _make_client(self) -> aiomqtt.Client:
        cfg = self._cfg
        kwargs: dict = {
            "hostname": cfg.broker_endpoint,
            "port": cfg.broker_port,
            "identifier": cfg.client_id,
            "keepalive": cfg.keepalive_seconds,
            "clean_session": False,
        }
        protocols = cfg.alpn_protocols()
        if protocols is not None:
            # ALPN path: MQTT is negotiated inside the TLS handshake, so the
            # store can connect on the HTTPS port (443) and no dedicated
            # outbound MQTT port (8883) needs to be opened on the firewall.
            kwargs["tls_context"] = self._build_ssl_context(protocols)
        else:
            if cfg.broker_port == 443 and cfg.auth_mode == "mtls":
                logger.warning(
                    "mqtt configured for port 443 with mtls but MQTT_USE_ALPN "
                    "is off; AWS IoT Core requires ALPN 'x-amzn-mqtt-ca' on 443 "
                    "and the handshake will fail. Set MQTT_USE_ALPN=true."
                )
            kwargs["tls_params"] = self._build_tls_params()
        if cfg.auth_mode == "userpass":
            kwargs["username"] = cfg.username
            kwargs["password"] = cfg.password
        return aiomqtt.Client(**kwargs)

    async def _run_supervisor(self) -> None:
        """Open the connection and keep it open across failures."""
        attempt = 0
        while not self._stop.is_set():
            try:
                client = self._make_client()
                async with client:
                    self._client = client
                    self._connected.set()
                    logger.info(
                        "mqtt connected endpoint=%s port=%d client_id=%s auth=%s alpn=%s",
                        self._cfg.broker_endpoint,
                        self._cfg.broker_port,
                        self._cfg.client_id,
                        self._cfg.auth_mode,
                        self._cfg.alpn_protocol if self._cfg.use_alpn else "off",
                    )
                    attempt = 0
                    # aiomqtt 2.x doesn't expose session_present directly; assume
                    # False on first connect so we re-subscribe, and True on
                    # subsequent reconnects if subscriptions are retained by the
                    # broker. Keep it simple: always re-subscribe on reconnect.
                    # This is safe even when the broker retained the session.
                    session_present = False
                    if self._on_connect is not None:
                        try:
                            await self._on_connect(session_present)
                        except Exception:  # noqa: BLE001
                            logger.exception("on_connect callback failed")
                    # Re-apply remembered subscriptions.
                    for topic_filter, qos in list(self._subscriptions):
                        await client.subscribe(topic_filter, qos=qos)
                        logger.info(
                            "mqtt re-subscribed topic=%s qos=%d",
                            topic_filter,
                            qos,
                        )
                    await self._pump_messages(client)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self._connected.clear()
                self._client = None
                if self._stop.is_set():
                    break
                delay = _backoff_with_jitter(attempt)
                attempt += 1
                logger.warning(
                    "mqtt connection lost: %s — reconnecting in %.1fs",
                    e,
                    delay,
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                    # If wait_for returned normally, stop was requested.
                    break
                except asyncio.TimeoutError:
                    continue
        self._connected.clear()
        self._client = None
        logger.info("mqtt supervisor exiting")

    async def _pump_messages(self, client: aiomqtt.Client) -> None:
        """Iterate messages from the client and deliver to ``on_message``."""
        async for message in client.messages:
            topic = str(message.topic)
            payload = message.payload
            if not isinstance(payload, (bytes, bytearray)):
                payload = str(payload).encode()
            try:
                await self._on_message(topic, bytes(payload))
            except Exception:  # noqa: BLE001
                logger.exception("on_message handler raised; continuing")


def _backoff_with_jitter(attempt: int) -> float:
    base = min(_BACKOFF_BASE * (2**attempt), _BACKOFF_CAP)
    jitter = base * _JITTER_FRAC
    # bandit B311 false positive: random drives seeded simulation data / retry jitter, not anything security-sensitive.
    return base + random.uniform(-jitter, jitter)  # nosec B311
