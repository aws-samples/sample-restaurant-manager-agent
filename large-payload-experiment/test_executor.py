# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Slice 2 — the chunk-aware executor driven by the repo's _FakeTransport pattern.

Proves the reassembler works inside the real Correlation-A round-trip shape:
publish a command, the (fake) store streams back N chunks — optionally shuffled,
duplicated, or dropped — and the executor returns exactly one reassembled tool
result to the waiter. No broker, no AWS.
"""

from __future__ import annotations

import json

from chunking import FaultInjector, chunk_result
from executor import ChunkAwareToolConnection

_SMALL = dict(chunk_bytes=4 * 1024, single_msg_budget=4 * 1024)


def _rows(n: int) -> dict:
    return {"status": "success", "tool_name": "get_transaction_history",
            "result": {"orders": [{"id": i, "v": i * 3} for i in range(n)]}}


class _ChunkingFakeTransport:
    """Like the repo's _FakeTransport, but the store responds with a *chunk
    stream* run through an optional fault filter before delivery."""

    def __init__(self, *, store_id, iot_endpoint, region, result, fault=None, **split_kw):
        self.store_id = store_id
        self._result = result
        self._fault = fault or (lambda chunks: chunks)
        self._split_kw = split_kw
        self.on_message = None

    def start(self, resp_filter, on_message):
        self.on_message = on_message

    def publish(self, topic, payload):
        cid = json.loads(payload.decode())["correlation_id"]
        chunks = chunk_result(self._result, cid, **{**_SMALL, **self._split_kw})
        for env in self._fault(chunks):
            self.on_message(f"stores/{self.store_id}/responses/{cid}",
                            json.dumps(env).encode("utf-8"))

    def close(self):
        pass


def _factory(**kw):
    def make(*, store_id, iot_endpoint, region):
        return _ChunkingFakeTransport(store_id=store_id, iot_endpoint=iot_endpoint,
                                      region=region, **kw)
    return make


def _conn(**kw):
    return ChunkAwareToolConnection(store_id="s1", iot_endpoint="ep", region="r",
                                    transport_factory=_factory(**kw))


def test_executor_reassembles_chunked_response() -> None:
    original = _rows(5000)
    conn = _conn(result=original)
    out = conn.round_trip(tool_name="get_transaction_history", domain="pos",
                          parameters={}, timeout_s=5.0)
    assert out == original


def test_executor_handles_shuffled_and_duplicated_stream() -> None:
    original = _rows(5000)
    fi = FaultInjector(seed=7)
    conn = _conn(result=original, fault=lambda c: fi.shuffle(fi.duplicate(c, times=6)))
    out = conn.round_trip(tool_name="get_transaction_history", domain="pos",
                          parameters={}, timeout_s=5.0)
    assert out == original


def test_executor_single_message_unchanged() -> None:
    original = {"status": "success", "result": {"temp_f": 38}}
    conn = _conn(result=original)  # small -> one chunk, total_chunks == 1
    out = conn.round_trip(tool_name="get_equipment_status", domain="equipment",
                          parameters={}, timeout_s=5.0)
    assert out == original


def test_executor_fails_loud_on_missing_chunk() -> None:
    fi = FaultInjector(seed=8)
    conn = _conn(result=_rows(5000), fault=fi.drop_one)
    out = conn.round_trip(tool_name="get_transaction_history", domain="pos",
                          parameters={}, timeout_s=0.3)
    assert out["status"] == "error"
    assert out["error"]["code"] in ("incomplete", "timeout")


def test_executor_fails_loud_on_corruption() -> None:
    conn = _conn(result=_rows(5000), fault=lambda c: FaultInjector.corrupt_one(c, seq=1))
    out = conn.round_trip(tool_name="get_transaction_history", domain="pos",
                          parameters={}, timeout_s=5.0)
    assert out["status"] == "error"
    assert out["error"]["code"] == "checksum_mismatch"
