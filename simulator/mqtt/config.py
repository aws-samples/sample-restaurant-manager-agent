# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""MQTT bridge configuration — loads environment variables into a typed model.

Broker-specific concerns (endpoint, port, TLS, auth mode) are isolated in this
module and in :mod:`simulator.mqtt.client`. Switching between Amazon MQ
(username/password) and AWS IoT Core (mTLS) is a matter of changing
``MQTT_AUTH_MODE`` and the associated environment variables — no code change.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class MQTTConnectionConfig(BaseModel):
    """Typed MQTT bridge configuration loaded from environment variables."""

    enabled: bool = False
    broker_endpoint: str = ""
    broker_port: int = 8883
    # ALPN lets the store connect over the HTTPS port (443) instead of 8883, so
    # no outbound-port firewall change is needed. AWS IoT Core negotiates MQTT +
    # X.509 client-cert auth on 443 via the ALPN protocol name ``x-amzn-mqtt-ca``.
    # Opt-in (default off => classic 8883 path, unchanged). See ``alpn_protocols``.
    use_alpn: bool = False
    alpn_protocol: str = "x-amzn-mqtt-ca"
    auth_mode: Literal["userpass", "mtls"] = "userpass"
    username: str | None = None
    password: str | None = None
    client_cert_path: str | None = None
    client_key_path: str | None = None
    ca_bundle_path: str | None = None
    store_id: str = ""
    keepalive_seconds: int = Field(default=60, ge=5, le=3600)
    dedup_cache_size: int = Field(default=1024, ge=16)
    dedup_cache_ttl_seconds: int = Field(default=1800, ge=60)
    outbound_buffer_size: int = Field(default=256, ge=16)
    inbound_queue_size: int = Field(default=64, ge=8)
    concurrent_tool_limit: int = Field(default=8, ge=1)
    shutdown_timeout_seconds: float = Field(default=5.0, ge=0.5)

    # --- Validation ---

    @model_validator(mode="after")
    def _check_required_when_enabled(self) -> "MQTTConnectionConfig":
        if not self.enabled:
            return self
        if not self.broker_endpoint:
            raise ValueError(
                "MQTT_BROKER_ENDPOINT is required when MQTT_ENABLED=true"
            )
        if not self.store_id:
            raise ValueError("STORE_ID is required when MQTT_ENABLED=true")

        if self.auth_mode == "userpass":
            if not self.username:
                raise ValueError(
                    "MQTT_USERNAME is required when MQTT_AUTH_MODE=userpass"
                )
            if not self.password:
                raise ValueError(
                    "MQTT_PASSWORD is required when MQTT_AUTH_MODE=userpass"
                )
        elif self.auth_mode == "mtls":
            if not self.client_cert_path:
                raise ValueError(
                    "MQTT_CLIENT_CERT_PATH is required when MQTT_AUTH_MODE=mtls"
                )
            if not self.client_key_path:
                raise ValueError(
                    "MQTT_CLIENT_KEY_PATH is required when MQTT_AUTH_MODE=mtls"
                )
            cert = Path(self.client_cert_path)
            key = Path(self.client_key_path)
            if not cert.is_file():
                raise ValueError(
                    f"MQTT_CLIENT_CERT_PATH does not exist or is not a file: {cert}"
                )
            if not key.is_file():
                raise ValueError(
                    f"MQTT_CLIENT_KEY_PATH does not exist or is not a file: {key}"
                )

        if self.ca_bundle_path:
            ca = Path(self.ca_bundle_path)
            if not ca.is_file():
                raise ValueError(
                    f"MQTT_CA_BUNDLE_PATH does not exist or is not a file: {ca}"
                )
        return self

    # --- Loader ---

    @classmethod
    def load_from_env(cls) -> "MQTTConnectionConfig":
        """Read environment variables and build a validated config.

        When ``MQTT_ENABLED`` is falsy, returns a disabled stub that skips
        field-by-field validation so the simulator can start without MQTT
        credentials configured. Any inconsistent field is still reported.
        """
        enabled = _truthy(os.environ.get("MQTT_ENABLED", "false"))
        raw = {
            "enabled": enabled,
            "broker_endpoint": os.environ.get("MQTT_BROKER_ENDPOINT", "").strip(),
            "broker_port": _int("MQTT_BROKER_PORT", 8883),
            "use_alpn": _truthy(os.environ.get("MQTT_USE_ALPN", "false")),
            "alpn_protocol": (
                os.environ.get("MQTT_ALPN_PROTOCOL", "x-amzn-mqtt-ca").strip()
                or "x-amzn-mqtt-ca"
            ),
            "auth_mode": os.environ.get("MQTT_AUTH_MODE", "userpass").strip().lower(),
            "username": _opt("MQTT_USERNAME"),
            "password": _opt("MQTT_PASSWORD"),
            "client_cert_path": _opt("MQTT_CLIENT_CERT_PATH"),
            "client_key_path": _opt("MQTT_CLIENT_KEY_PATH"),
            "ca_bundle_path": _opt("MQTT_CA_BUNDLE_PATH"),
            "store_id": os.environ.get("STORE_ID", "").strip(),
            "keepalive_seconds": _int("MQTT_KEEPALIVE_SECONDS", 60),
            "dedup_cache_size": _int("MQTT_DEDUP_CACHE_SIZE", 1024),
            "dedup_cache_ttl_seconds": _int("MQTT_DEDUP_CACHE_TTL_SECONDS", 1800),
            "outbound_buffer_size": _int("MQTT_OUTBOUND_BUFFER_SIZE", 256),
            "inbound_queue_size": _int("MQTT_INBOUND_QUEUE_SIZE", 64),
            "concurrent_tool_limit": _int("MQTT_CONCURRENT_TOOL_LIMIT", 8),
            "shutdown_timeout_seconds": _float("MQTT_SHUTDOWN_TIMEOUT_SECONDS", 5.0),
        }
        return cls(**raw)

    # --- Convenience ---

    @property
    def client_id(self) -> str:
        """Stable MQTT client id used for persistent session continuity.

        Equals ``store_id`` so it matches the IoT Core thing name, which is a
        prerequisite for ``${iot:Connection.Thing.ThingName}`` thing-policy
        variables (spec 020 R1.3 decision A). For Amazon MQ the specific value
        is immaterial (it is only a session identifier), so this is safe for
        both transports.
        """
        return self.store_id

    def alpn_protocols(self) -> list[str] | None:
        """ALPN protocol list to negotiate, or ``None`` for the classic path.

        Returns ``[alpn_protocol]`` (default ``["x-amzn-mqtt-ca"]``) when
        ``use_alpn`` is set — the client then connects over TLS on the
        configured port (typically 443) and the broker selects MQTT via ALPN.
        Returns ``None`` when ALPN is off, so the client keeps the plain
        port-8883 TLS path with no ALPN extension.
        """
        if not self.use_alpn:
            return None
        return [self.alpn_protocol]

    def topic_commands_wildcard(self) -> str:
        return f"stores/{self.store_id}/commands/+"

    def topic_response(self, correlation_id: str) -> str:
        return f"stores/{self.store_id}/responses/{correlation_id}"

    def topic_dead_letter(self) -> str:
        return f"stores/{self.store_id}/responses/_invalid"

    def topic_event(self, domain: str) -> str:
        """Store->cloud event topic (spec 023), e.g. proactive alerts."""
        return f"stores/{self.store_id}/events/{domain}"

    def topic_notifications_wildcard(self) -> str:
        """Cloud->store proactive-notification subscription (spec 023 Task 6)."""
        return f"stores/{self.store_id}/notifications/+"


# --- env parsing helpers ---

def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _opt(name: str) -> str | None:
    v = os.environ.get(name)
    if v is None:
        return None
    v = v.strip()
    return v or None


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from e


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be a number, got {raw!r}") from e
