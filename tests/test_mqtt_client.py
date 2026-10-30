# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the store-gateway MQTT ALPN (port-443) path and the classic 8883 path.

Covers the opt-in ``MQTT_USE_ALPN`` capability added so the store can connect to
AWS IoT Core over the HTTPS port (443) using the ``x-amzn-mqtt-ca`` ALPN protocol
name, removing the outbound-8883 firewall prerequisite. The existing 8883
``TLSParameters`` path must remain unchanged (ALPN off by default).
"""

from __future__ import annotations

import logging
import ssl
from unittest import mock

import aiomqtt

from simulator.mqtt.client import MQTTClient
from simulator.mqtt.config import MQTTConnectionConfig


async def _noop(topic: str, payload: bytes) -> None:  # pragma: no cover - stub
    return None


# Placeholder paths: enabled=False skips cert-file validation and the test
# replaces load_cert_chain, so nothing is read from disk.
FAKE_CERT_PATH = "/etc/restaurant-sim/certs/device.pem.crt"
FAKE_KEY_PATH = "/etc/restaurant-sim/certs/device.private.key"


def _mtls_cfg(**overrides) -> MQTTConnectionConfig:
    """An mtls config with ``enabled=False`` so cert-file validation is skipped."""
    base = dict(
        enabled=False,
        store_id="sim-store-001",
        auth_mode="mtls",
        broker_endpoint="xxxx-ats.iot.us-west-2.amazonaws.com",
        client_cert_path=FAKE_CERT_PATH,
        client_key_path=FAKE_KEY_PATH,
    )
    base.update(overrides)
    return MQTTConnectionConfig(**base)


# --- config: ALPN selection ------------------------------------------------

def test_alpn_off_by_default():
    cfg = MQTTConnectionConfig(enabled=False, store_id="s")
    assert cfg.use_alpn is False
    assert cfg.alpn_protocol == "x-amzn-mqtt-ca"
    assert cfg.broker_port == 8883
    assert cfg.alpn_protocols() is None


def test_alpn_protocols_when_enabled():
    cfg = MQTTConnectionConfig(enabled=False, store_id="s", use_alpn=True)
    assert cfg.alpn_protocols() == ["x-amzn-mqtt-ca"]


def test_alpn_protocol_override():
    cfg = MQTTConnectionConfig(
        enabled=False, store_id="s", use_alpn=True, alpn_protocol="mqtt"
    )
    assert cfg.alpn_protocols() == ["mqtt"]


def test_load_from_env_enables_alpn(monkeypatch):
    monkeypatch.setenv("MQTT_ENABLED", "false")
    monkeypatch.setenv("MQTT_USE_ALPN", "true")
    monkeypatch.setenv("MQTT_BROKER_PORT", "443")
    monkeypatch.setenv("MQTT_AUTH_MODE", "mtls")
    monkeypatch.setenv("STORE_ID", "sim-store-001")
    cfg = MQTTConnectionConfig.load_from_env()
    assert cfg.use_alpn is True
    assert cfg.broker_port == 443
    assert cfg.alpn_protocols() == ["x-amzn-mqtt-ca"]


def test_load_from_env_alpn_off_by_default(monkeypatch):
    monkeypatch.delenv("MQTT_USE_ALPN", raising=False)
    monkeypatch.delenv("MQTT_ALPN_PROTOCOL", raising=False)
    monkeypatch.setenv("MQTT_ENABLED", "false")
    cfg = MQTTConnectionConfig.load_from_env()
    assert cfg.use_alpn is False
    assert cfg.broker_port == 8883
    assert cfg.alpn_protocol == "x-amzn-mqtt-ca"
    assert cfg.alpn_protocols() is None


# --- client: SSLContext construction for the ALPN path ---------------------

def test_build_ssl_context_sets_alpn_and_loads_client_cert(monkeypatch):
    cfg = _mtls_cfg(use_alpn=True, broker_port=443)
    client = MQTTClient(cfg, on_message=_noop)

    loaded: dict = {}
    alpn: dict = {}
    monkeypatch.setattr(
        ssl.SSLContext,
        "load_cert_chain",
        lambda self, certfile, keyfile: loaded.update(cert=certfile, key=keyfile),
    )
    monkeypatch.setattr(
        ssl.SSLContext,
        "set_alpn_protocols",
        lambda self, protocols: alpn.update(p=list(protocols)),
    )

    ctx = client._build_ssl_context(cfg.alpn_protocols())

    assert isinstance(ctx, ssl.SSLContext)
    # Secure server-auth defaults must be preserved (verify broker + hostname).
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    # Client cert loaded for mTLS, ALPN advertises the IoT protocol name.
    assert loaded == {"cert": FAKE_CERT_PATH, "key": FAKE_KEY_PATH}
    assert alpn == {"p": ["x-amzn-mqtt-ca"]}


# Fake values live in neutrally named constants: Bandit B105/B106 flag a string
# *literal* bound to a secret-sounding name, even in tests.
_FAKE_PW = "p"


def test_build_ssl_context_userpass_skips_client_cert(monkeypatch):
    cfg = MQTTConnectionConfig(
        enabled=False,
        store_id="s",
        auth_mode="userpass",
        username="u",
        password=_FAKE_PW,
        use_alpn=True,
        broker_port=443,
    )
    client = MQTTClient(cfg, on_message=_noop)

    load_calls: list = []
    alpn: dict = {}
    monkeypatch.setattr(
        ssl.SSLContext,
        "load_cert_chain",
        lambda self, **kw: load_calls.append(kw),
    )
    monkeypatch.setattr(
        ssl.SSLContext,
        "set_alpn_protocols",
        lambda self, protocols: alpn.update(p=list(protocols)),
    )

    ctx = client._build_ssl_context(cfg.alpn_protocols())

    assert isinstance(ctx, ssl.SSLContext)
    assert load_calls == []  # no client cert loaded for userpass
    assert alpn == {"p": ["x-amzn-mqtt-ca"]}


# --- client: _make_client branch selection ---------------------------------

async def test_make_client_classic_8883_uses_tls_params():
    cfg = _mtls_cfg(use_alpn=False, broker_port=8883)
    client = MQTTClient(cfg, on_message=_noop)
    client._build_ssl_context = mock.MagicMock()
    client._build_tls_params = mock.MagicMock(return_value=aiomqtt.TLSParameters())

    c = client._make_client()

    assert c._port == 8883
    client._build_tls_params.assert_called_once()
    client._build_ssl_context.assert_not_called()


async def test_make_client_alpn_443_uses_tls_context():
    cfg = _mtls_cfg(use_alpn=True, broker_port=443)
    client = MQTTClient(cfg, on_message=_noop)
    real_ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    client._build_ssl_context = mock.MagicMock(return_value=real_ctx)
    client._build_tls_params = mock.MagicMock()

    c = client._make_client()

    assert c._port == 443
    client._build_ssl_context.assert_called_once_with(["x-amzn-mqtt-ca"])
    client._build_tls_params.assert_not_called()


async def test_make_client_443_without_alpn_warns(caplog):
    cfg = _mtls_cfg(use_alpn=False, broker_port=443)
    client = MQTTClient(cfg, on_message=_noop)
    client._build_tls_params = mock.MagicMock(return_value=aiomqtt.TLSParameters())

    with caplog.at_level(logging.WARNING, logger="simulator.mqtt.client"):
        c = client._make_client()

    assert c._port == 443
    client._build_tls_params.assert_called_once()
    assert "MQTT_USE_ALPN" in caplog.text
