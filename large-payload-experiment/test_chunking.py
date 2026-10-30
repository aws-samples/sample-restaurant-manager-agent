# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Slice 1 — adversarial unit tests for the chunking producer + reassembler.

No broker, no AWS, no network. These prove the risky half of the design: the
reassembler stays correct under out-of-order, duplicate, missing, and corrupt
chunk streams that IoT Core's weak delivery guarantees make possible.

Run: pytest large-payload-experiment/test_chunking.py -v
"""

from __future__ import annotations

import pytest

from chunking import (
    FaultInjector,
    Reassembler,
    ResultTooLargeError,
    chunk_result,
    metered_units,
)


def _big_result(n_rows: int = 4000) -> dict:
    """A result that will not fit in one message even after gzip — modestly
    random-ish rows so compression is realistic, not pathological."""
    return {
        "status": "success",
        "tool_name": "get_transaction_history",
        "result": {
            "orders": [
                {"order_id": i, "ticket": 10.0 + (i % 97) * 0.37, "server": f"srv_{i % 23}",
                 "items": (i % 11) + 1, "ts": f"2026-07-21T{(i % 24):02d}:{(i % 60):02d}:00Z"}
                for i in range(n_rows)
            ]
        },
    }


# Small thresholds force a multi-chunk split regardless of how well the sample
# data compresses — we are exercising the reassembler, not the exact 90 KB
# boundary. Realistic-size boundary behavior is covered by the producer tests.
_SMALL = dict(chunk_bytes=4 * 1024, single_msg_budget=4 * 1024)


def _split(result: dict, cid: str, **kw) -> list[dict]:
    return chunk_result(result, cid, **{**_SMALL, **kw})


def _drain(chunks: list[dict], reassembler: Reassembler):
    """Feed every chunk; return the first terminal (complete/error) result, or
    the last pending result if none terminates."""
    last = None
    for env in chunks:
        last = reassembler.offer(env)
        if last.status in ("complete", "error"):
            return last
    return last


# ---------------------------------------------------------------------------
# Producer shape
# ---------------------------------------------------------------------------


def test_small_result_is_single_message() -> None:
    chunks = chunk_result({"status": "success", "result": {"temp_f": 38}}, "c-small")
    assert len(chunks) == 1
    assert chunks[0]["part"] == {"seq": 0, "total_chunks": 1}
    assert chunks[0]["final"] is True


def test_large_result_splits_into_many_chunks() -> None:
    # Uncompressed big result exceeds the real 100 KB single-message budget.
    chunks = chunk_result(_big_result(), "c-big", compress=False)
    assert len(chunks) > 1
    assert chunks[-1]["final"] is True
    assert all(c["final"] is False for c in chunks[:-1])
    seqs = [c["part"]["seq"] for c in chunks]
    assert seqs == list(range(len(chunks)))
    assert {c["part"]["total_chunks"] for c in chunks} == {len(chunks)}


def test_over_cap_raises_on_producer() -> None:
    with pytest.raises(ResultTooLargeError):
        chunk_result(_big_result(20000), "c-huge", compress=False, max_chunks=4)


# ---------------------------------------------------------------------------
# Happy path + single message
# ---------------------------------------------------------------------------


def test_single_message_round_trip() -> None:
    original = {"status": "success", "result": {"temp_f": 38, "alerts": ["door_ajar"]}}
    r = _drain(chunk_result(original, "c1"), Reassembler())
    assert r.status == "complete"
    assert r.result == original


def test_in_order_round_trip() -> None:
    original = _big_result()
    r = _drain(_split(original, "c2"), Reassembler())
    assert r.status == "complete"
    assert r.result == original


# ---------------------------------------------------------------------------
# Adversarial: out of order, duplicates, missing, corrupt
# ---------------------------------------------------------------------------


def test_out_of_order_reassembles_by_seq() -> None:
    original = _big_result()
    chunks = _split(original, "c3")
    shuffled = FaultInjector(seed=1).shuffle(chunks)
    assert [c["part"]["seq"] for c in shuffled] != list(range(len(chunks)))  # actually shuffled
    r = _drain(shuffled, Reassembler())
    assert r.status == "complete"
    assert r.result == original


def test_duplicate_chunks_are_idempotent() -> None:
    original = _big_result()
    chunks = _split(original, "c4")
    noisy = FaultInjector(seed=2).duplicate(chunks, times=5)
    assert len(noisy) == len(chunks) + 5
    r = _drain(noisy, Reassembler())
    assert r.status == "complete"
    assert r.result == original


def test_out_of_order_and_duplicated_together() -> None:
    original = _big_result()
    fi = FaultInjector(seed=3)
    chunks = fi.shuffle(fi.duplicate(_split(original, "c5"), times=8))
    r = _drain(chunks, Reassembler())
    assert r.status == "complete"
    assert r.result == original


def test_missing_chunk_never_completes_then_times_out() -> None:
    chunks = _split(_big_result(), "c6")
    partial = FaultInjector(seed=4).drop_one(chunks)
    re = Reassembler(timeout_s=0.0)  # everything is immediately "expired"
    assert _drain(partial, re).status == "pending"  # never completes on its own
    expired = re.sweep_expired()
    assert len(expired) == 1
    assert expired[0].status == "error"
    assert expired[0].error["code"] == "incomplete"


def test_corrupt_chunk_fails_checksum() -> None:
    chunks = _split(_big_result(), "c7")
    corrupt = FaultInjector.corrupt_one(chunks, seq=2)
    r = _drain(corrupt, Reassembler())
    assert r.status == "error"
    assert r.error["code"] == "checksum_mismatch"


def test_over_cap_rejected_by_reassembler() -> None:
    # Producer allows many chunks; a stricter reassembler cap must reject them.
    chunks = _split(_big_result(), "c8", max_chunks=64)
    assert len(chunks) > 2
    r = _drain(chunks, Reassembler(max_chunks=2))
    assert r.status == "error"
    assert r.error["code"] == "too_large"


# ---------------------------------------------------------------------------
# Isolation + encoding
# ---------------------------------------------------------------------------


def test_two_interleaved_transfers_do_not_cross_contaminate() -> None:
    a, b = _big_result(3000), _big_result(3500)
    ca = _split(a, "cA")
    cb = _split(b, "cB")
    fi = FaultInjector(seed=5)
    mixed = fi.shuffle(ca + cb)  # interleave both transfers, shuffled
    re = Reassembler()
    done = {}
    for env in mixed:
        r = re.offer(env)
        if r.status == "complete":
            done[r.correlation_id] = r.result
        assert r.status != "error"
    assert done["cA"] == a
    assert done["cB"] == b


def test_gzip_round_trip_and_encoding_flag() -> None:
    original = _big_result()  # highly compressible
    chunks = _split(original, "c9", compress=True)
    assert chunks[0]["encoding"] == "gzip"
    assert _drain(chunks, Reassembler()).result == original


def test_identity_encoding_when_compression_disabled() -> None:
    original = {"status": "success", "result": {"x": 1}}
    chunks = chunk_result(original, "c10", compress=False)
    assert chunks[0]["encoding"] == "identity"
    assert _drain(chunks, Reassembler()).result == original


# ---------------------------------------------------------------------------
# Metering — empirical backing for the cost / Basic-Ingest argument
# ---------------------------------------------------------------------------


def test_metering_counts_5kb_increments() -> None:
    chunks = chunk_result(_big_result(6000), "c11", compress=False)
    units = metered_units(chunks)
    # Each ~90 KB raw chunk is ~26 units; the set must bill far above its count.
    assert units > len(chunks)
    per_chunk = units / len(chunks)
    assert per_chunk > 20  # ~26 units for a full chunk, confirming per-5KB metering
