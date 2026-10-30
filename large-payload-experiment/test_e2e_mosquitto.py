# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Slice 3 — end-to-end chunking over a real MQTT broker (local Mosquitto).

Proves the wiring against a real broker and QoS 1: a store-side producer
publishes chunk envelopes to ``stores/{id}/responses/{cid}``; a cloud-side
subscriber feeds each to the Reassembler and recovers exactly one result. Runs
producer→broker→reassembler over genuine MQTT, not an in-process fake.

Auto-skips when paho-mqtt is not installed or no broker is reachable on
localhost:1883, so it never breaks a CI run that has neither. To run it:

    pip install paho-mqtt
    docker compose -f large-payload-experiment/docker-compose.yml up -d
    ../.venv/bin/python -m pytest large-payload-experiment/test_e2e_mosquitto.py -v

Mosquitto is MQTT-faithful for topics/QoS/per-message delivery but does NOT
reproduce IoT Core's 128 KB rejection or 5 KB metering — this validates logic
and wiring, not those AWS-specific behaviors.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import uuid

import pytest

from chunking import Reassembler, chunk_result

paho = pytest.importorskip("paho.mqtt.client", reason="paho-mqtt not installed")

BROKER_HOST, BROKER_PORT = "localhost", 1883


def _broker_up() -> bool:
    try:
        with socket.create_connection((BROKER_HOST, BROKER_PORT), timeout=1.0):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _broker_up(),
    reason="no MQTT broker on localhost:1883 (run: docker compose -f "
    "large-payload-experiment/docker-compose.yml up -d)",
)

STORE_ID = "e2e-store"
_SMALL = dict(chunk_bytes=8 * 1024, single_msg_budget=8 * 1024)


def _client(cid_suffix: str):
    # paho-mqtt 2.x requires the callback API version argument.
    return paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=f"exp-{cid_suffix}-{uuid.uuid4().hex[:6]}")


def _rows(n: int) -> dict:
    return {"status": "success", "tool_name": "get_transaction_history",
            "result": {"orders": [{"id": i, "ticket": 10 + i % 50} for i in range(n)]}}


def _run_round_trip(result: dict, *, split_kw: dict) -> dict:
    """Subscribe (cloud), publish chunks (store) over the real broker, and
    return the reassembled result (or a structured error)."""
    cid = str(uuid.uuid4())
    topic = f"stores/{STORE_ID}/responses/{cid}"
    reassembler = Reassembler(timeout_s=10.0)
    done: dict = {}
    ready = threading.Event()
    got = threading.Event()

    sub = _client("sub")

    def on_connect(c, u, flags, rc, props=None):
        c.subscribe(f"stores/{STORE_ID}/responses/+", qos=1)
        ready.set()

    def on_message(c, u, msg):
        env = json.loads(msg.payload.decode("utf-8"))
        outcome = reassembler.offer(env)
        if outcome.status in ("complete", "error"):
            done["outcome"] = outcome
            got.set()

    sub.on_connect = on_connect
    sub.on_message = on_message
    sub.connect(BROKER_HOST, BROKER_PORT, keepalive=30)
    sub.loop_start()
    assert ready.wait(5), "subscriber did not connect"
    # nosemgrep: arbitrary-sleep
    time.sleep(0.2)  # let the SUBACK settle before publishing

    pub = _client("pub")
    pub.connect(BROKER_HOST, BROKER_PORT, keepalive=30)
    pub.loop_start()
    for env in chunk_result(result, cid, **{**_SMALL, **split_kw}):
        pub.publish(topic, json.dumps(env).encode("utf-8"), qos=1).wait_for_publish(5)

    assert got.wait(10), "no terminal reassembly result within timeout"
    pub.loop_stop(); pub.disconnect()
    sub.loop_stop(); sub.disconnect()

    outcome = done["outcome"]
    return outcome.result if outcome.status == "complete" else {"status": "error", "error": outcome.error}


def test_e2e_single_message_over_broker() -> None:
    original = {"status": "success", "result": {"temp_f": 38}}
    assert _run_round_trip(original, split_kw={}) == original


def test_e2e_multichunk_over_broker() -> None:
    original = _rows(8000)
    assert _run_round_trip(original, split_kw={}) == original
