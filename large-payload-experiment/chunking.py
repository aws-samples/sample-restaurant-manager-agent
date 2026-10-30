# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Chunking large tool results over MQTT — reference implementation for the
local experiment.

Implements the design in
``docs/solution-guidance/large-payload-chunking-design.md``: a store-side
**producer** that gzip-compresses a tool-result envelope, splits it into
ordered segments that each fit under the 128 KB IoT Core message limit, and a
cloud-side **reassembler** that buffers by ``correlation_id``, dedups on
``(correlation_id, seq)``, verifies a SHA-256 over the full payload, gunzips,
and returns exactly one result — failing loudly on an incomplete or corrupt
set.

This module is intentionally free of AWS / MQTT-client imports so the logic can
be unit-tested with no broker and no network. The experiment's executor and
end-to-end tests wire it to the real transports.

Chunk envelope (one JSON object per MQTT message on
``stores/{store_id}/responses/{correlation_id}``)::

    {
      "correlation_id": "c-4471",
      "part":     {"seq": 3, "total_chunks": 27},
      "encoding": "gzip",              # or "identity"
      "checksum": "sha256:9f8e...",    # digest of the FULL reassembled payload
      "final":    false,               # true only on the highest seq
      "data":     "<base64 chunk bytes>"
    }
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import time
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Tunables (mirror the design doc's bounds)
# ---------------------------------------------------------------------------

#: IoT Core hard limit on an MQTT publish payload.
IOT_MAX_MESSAGE_BYTES = 128 * 1024
#: Raw bytes per chunk before base64/JSON framing. base64 inflates ~33%, so
#: ~90 KB raw keeps the encoded message comfortably under 128 KB.
DEFAULT_CHUNK_BYTES = 90 * 1024
#: Below this encoded size a result is sent as a single message (total_chunks=1).
DEFAULT_SINGLE_MSG_BUDGET = 100 * 1024
#: Explicit ceiling: refuse to stream a result that needs more chunks than this.
DEFAULT_MAX_CHUNKS = 64
#: IoT Core meters messages in 5 KB increments (for the cost counter).
METERING_INCREMENT_BYTES = 5 * 1024


class ResultTooLargeError(Exception):
    """A result would require more than ``max_chunks`` segments to stream."""


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Producer (store side)
# ---------------------------------------------------------------------------


def encode_body(result: dict, *, compress: bool = True) -> tuple[bytes, str]:
    """Serialize and (optionally) gzip a tool-result dict.

    Returns ``(body, encoding)`` where ``encoding`` is ``"gzip"`` or
    ``"identity"``. Compression is skipped automatically when it does not help
    (already-compressed payloads), mirroring the store's content-aware choice.
    """
    raw = json.dumps(result, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if not compress:
        return raw, "identity"
    # mtime=0 for deterministic output (stable checksum across runs/tests).
    gz = gzip.compress(raw, mtime=0)
    if len(gz) < len(raw):
        return gz, "gzip"
    return raw, "identity"


def chunk_result(
    result: dict,
    correlation_id: str,
    *,
    compress: bool = True,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    single_msg_budget: int = DEFAULT_SINGLE_MSG_BUDGET,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
) -> list[dict]:
    """Split a tool result into one or more chunk envelopes.

    A small result yields a single envelope with ``total_chunks == 1`` — the
    same shape the reassembler consumes for the multi-chunk case, so there is
    one code path on the receiving side.

    Raises :class:`ResultTooLargeError` if the (compressed) body would need
    more than ``max_chunks`` segments.
    """
    body, encoding = encode_body(result, compress=compress)
    checksum = _sha256(body)

    # Decide single-message vs chunked on the ENCODED size of a probe envelope,
    # so envelope + base64 overhead is accounted for, not just the raw bytes.
    if len(_envelope_bytes(correlation_id, 0, 1, encoding, checksum, True, body)) <= single_msg_budget:
        return [_envelope(correlation_id, 0, 1, encoding, checksum, True, body)]

    segments = [body[i : i + chunk_bytes] for i in range(0, len(body), chunk_bytes)]
    total = len(segments)
    if total > max_chunks:
        raise ResultTooLargeError(
            f"result needs {total} chunks (> max_chunks={max_chunks})"
        )
    return [
        _envelope(correlation_id, seq, total, encoding, checksum, seq == total - 1, seg)
        for seq, seg in enumerate(segments)
    ]


def _envelope(
    cid: str, seq: int, total: int, encoding: str, checksum: str, final: bool, data: bytes
) -> dict:
    return {
        "correlation_id": cid,
        "part": {"seq": seq, "total_chunks": total},
        "encoding": encoding,
        "checksum": checksum,
        "final": final,
        "data": base64.b64encode(data).decode("ascii"),
    }


def _envelope_bytes(
    cid: str, seq: int, total: int, encoding: str, checksum: str, final: bool, data: bytes
) -> bytes:
    return json.dumps(_envelope(cid, seq, total, encoding, checksum, final, data)).encode("utf-8")


# ---------------------------------------------------------------------------
# Reassembler (cloud side)
# ---------------------------------------------------------------------------


@dataclass
class _Buffer:
    total_chunks: int | None = None
    checksum: str | None = None
    encoding: str | None = None
    final_seen: bool = False
    parts: dict[int, bytes] = field(default_factory=dict)
    created_at: float = field(default_factory=time.monotonic)


@dataclass
class ReassembleResult:
    """Outcome of feeding one chunk to the reassembler."""

    #: "pending" (need more chunks), "complete", or "error".
    status: str
    correlation_id: str
    #: The reassembled tool-result dict — only on status == "complete".
    result: dict | None = None
    #: Structured error — only on status == "error".
    error: dict | None = None


class Reassembler:
    """Buffers chunks per ``correlation_id`` and yields one result when complete.

    Correctness never depends on arrival order or on any transport "duplicate"
    flag: reordering is by ``seq``, dedup is content-addressed on
    ``(correlation_id, seq)``, and integrity is verified with the SHA-256 the
    producer stamped over the full payload. Incomplete sets are reaped by
    :meth:`sweep_expired` and surfaced as a loud structured error.
    """

    def __init__(self, *, max_chunks: int = DEFAULT_MAX_CHUNKS, timeout_s: float = 30.0) -> None:
        self._max_chunks = max_chunks
        self._timeout_s = timeout_s
        self._buffers: dict[str, _Buffer] = {}

    def offer(self, envelope: dict) -> ReassembleResult:
        """Feed one chunk envelope. Returns pending / complete / error."""
        cid = envelope["correlation_id"]
        part = envelope["part"]
        seq = part["seq"]
        total = part["total_chunks"]

        # REVIEW (cravinho, 2026-08-18): envelope fields from the wire are trusted
        # structurally. A malformed producer (or a hostile publisher on the store's
        # response topic) can send seq/total that are non-int, negative, or
        # seq >= total. Consequences worth checking: (1) a duplicate/echo with the
        # correct `total` but a seq the buffer never expects can satisfy
        # `len(buf.parts) == total_chunks` while missing a real index, so the
        # b"".join(... range(total_chunks)) below KeyErrors (uncaught, crashes the
        # subscriber) rather than returning a structured error; (2) `total` is not
        # checked to be a positive int, so total=0 completes on the final marker
        # with an empty body. Validate seq/total types and 0 <= seq < total here
        # before use.
        if total > self._max_chunks:
            self._buffers.pop(cid, None)
            return self._err(cid, "too_large", f"result exceeds max_chunks={self._max_chunks}")

        buf = self._buffers.setdefault(cid, _Buffer())

        # First chunk to arrive sets the invariants; later chunks must agree.
        if buf.total_chunks is None:
            buf.total_chunks = total
            buf.checksum = envelope["checksum"]
            buf.encoding = envelope.get("encoding", "identity")
        elif total != buf.total_chunks or envelope["checksum"] != buf.checksum:
            self._buffers.pop(cid, None)
            return self._err(cid, "inconsistent_chunk", "chunk metadata disagrees within a transfer")

        # Idempotent insert — a re-delivered chunk (QoS 1) is simply dropped.
        if seq not in buf.parts:
            try:
                buf.parts[seq] = base64.b64decode(envelope["data"])
            except (ValueError, TypeError):
                self._buffers.pop(cid, None)
                return self._err(cid, "bad_chunk", f"chunk {seq} is not valid base64")
        if envelope.get("final"):
            buf.final_seen = True

        # Complete only when every index is present AND the final marker is seen.
        if not (buf.final_seen and len(buf.parts) == buf.total_chunks):
            return ReassembleResult(status="pending", correlation_id=cid)

        self._buffers.pop(cid, None)
        body = b"".join(buf.parts[i] for i in range(buf.total_chunks))
        if _sha256(body) != buf.checksum:
            return self._err(cid, "checksum_mismatch", "reassembled payload failed integrity check")
        try:
            raw = gzip.decompress(body) if buf.encoding == "gzip" else body
            result = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError) as exc:
            return self._err(cid, "decode_error", f"could not decode reassembled payload: {exc}")
        return ReassembleResult(status="complete", correlation_id=cid, result=result)

    def sweep_expired(self, *, now: float | None = None) -> list[ReassembleResult]:
        """Reap transfers whose buffer has outlived ``timeout_s``.

        Returns one loud ``error`` result per incomplete transfer so the caller
        can fail the corresponding tool call rather than hang forever.
        """
        now = time.monotonic() if now is None else now
        expired = [
            cid for cid, buf in self._buffers.items() if now - buf.created_at > self._timeout_s
        ]
        out: list[ReassembleResult] = []
        for cid in expired:
            buf = self._buffers.pop(cid)
            have, want = len(buf.parts), buf.total_chunks or "?"
            out.append(self._err(cid, "incomplete", f"reassembly timed out: {have}/{want} chunks"))
        return out

    @property
    def pending_count(self) -> int:
        return len(self._buffers)

    @staticmethod
    def _err(cid: str, code: str, message: str) -> ReassembleResult:
        return ReassembleResult(
            status="error", correlation_id=cid, error={"code": code, "message": message}
        )


# ---------------------------------------------------------------------------
# Fault injector — reproduce IoT Core's weak guarantees deterministically
# ---------------------------------------------------------------------------


class FaultInjector:
    """Deterministically reorder / duplicate / drop / corrupt a chunk stream.

    IoT Core guarantees neither ordering nor exactly-once delivery, so the
    reassembler must be correct under all of these. Seeded from an int for
    reproducible tests (the workflow rule against Math.random does not apply
    here — this is ordinary test tooling, seeded explicitly).
    """

    def __init__(self, *, seed: int = 0) -> None:
        import random

        # bandit B311 false positive: random drives seeded simulation data / retry jitter, not anything security-sensitive.
        self._rng = random.Random(seed)  # nosec B311

    def shuffle(self, chunks: list[dict]) -> list[dict]:
        out = list(chunks)
        self._rng.shuffle(out)
        return out

    def duplicate(self, chunks: list[dict], *, times: int = 1) -> list[dict]:
        out = list(chunks)
        for _ in range(times):
            if not chunks:
                break
            out.insert(self._rng.randrange(len(out) + 1), self._rng.choice(chunks))
        return out

    def drop_one(self, chunks: list[dict]) -> list[dict]:
        if len(chunks) <= 1:
            return []
        idx = self._rng.randrange(len(chunks))
        return [c for i, c in enumerate(chunks) if i != idx]

    @staticmethod
    def corrupt_one(chunks: list[dict], seq: int) -> list[dict]:
        """Flip the payload of chunk ``seq`` without touching its checksum field,
        so the transfer-level SHA-256 must catch it."""
        out = [dict(c, part=dict(c["part"])) for c in chunks]
        target = next(c for c in out if c["part"]["seq"] == seq)
        bad = base64.b64decode(target["data"])[::-1] or b"x"
        target["data"] = base64.b64encode(bad).decode("ascii")
        return out


# ---------------------------------------------------------------------------
# Metering counter — empirical backing for the Basic-Ingest cost argument
# ---------------------------------------------------------------------------


def metered_units(chunks: list[dict]) -> int:
    """IoT Core bills each message in 5 KB increments; total billable units for
    a chunk set. A 128 KB message is ~26 units, so this makes the per-message
    cost of chunking concrete — and why chunking is a rare fallback (compress
    and aggregate first) rather than a default path."""
    total = 0
    for env in chunks:
        size = len(json.dumps(env).encode("utf-8"))
        total += -(-size // METERING_INCREMENT_BYTES)  # ceil division
    return total
