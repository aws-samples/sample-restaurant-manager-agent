# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Slice 2 — a chunk-aware Correlation-A executor.

This mirrors the production ``_ToolConnection`` in
``simulator/api/harness_tools.py`` and shows exactly where chunk reassembly
slots in: its ``_on_message`` feeds every response envelope to a
:class:`~chunking.Reassembler` and only fulfils the waiter once a transfer is
complete (or errors). A single-message result (``total_chunks == 1``) flows
through the same path and completes on the first chunk, so the non-chunked case
is unchanged.

The transport is injected exactly like the production class
(``transport_factory``), so this is driven in tests by the same ``_FakeTransport``
pattern the repo already uses — no broker, no AWS.
"""

from __future__ import annotations

import json
import queue
import threading
import uuid

from chunking import Reassembler


class ChunkAwareToolConnection:
    """Pooled Correlation-A round-trip with chunk reassembly in ``_on_message``."""

    def __init__(self, *, store_id, iot_endpoint, region, transport_factory,
                 max_chunks=64, timeout_s=30.0):
        self.store_id = store_id
        self.iot_endpoint = iot_endpoint
        self.region = region
        self._transport_factory = transport_factory
        self._resp_filter = f"stores/{store_id}/responses/+"
        self._waiters_lock = threading.Lock()
        self._waiters: dict[str, "queue.Queue[dict]"] = {}
        self._transport = None
        self._reassembler = Reassembler(max_chunks=max_chunks, timeout_s=timeout_s)

    # -- response path: reassemble, then route one result to the waiter --------
    def _on_message(self, topic, payload) -> None:  # noqa: ANN001
        cid = topic.rsplit("/", 1)[-1]
        try:
            env = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._fulfil(cid, {"status": "error",
                               "error": {"code": "unparseable", "message": "bad response"}})
            return

        # A legacy (non-chunked) envelope has no "part"; treat it as a single
        # complete message so old producers keep working unchanged.
        if "part" not in env:
            self._fulfil(cid, env)
            return

        outcome = self._reassembler.offer(env)
        if outcome.status == "complete":
            self._fulfil(outcome.correlation_id, outcome.result)
        elif outcome.status == "error":
            self._fulfil(outcome.correlation_id,
                         {"status": "error", "error": outcome.error})
        # pending -> wait for more chunks

    def _fulfil(self, cid: str, env: dict) -> None:
        with self._waiters_lock:
            waiter = self._waiters.get(cid)
        if waiter is not None:
            try:
                waiter.put_nowait(env)
            except queue.Full:
                pass

    def _ensure_started(self):
        if self._transport is None:
            t = self._transport_factory(
                store_id=self.store_id, iot_endpoint=self.iot_endpoint, region=self.region)
            t.start(self._resp_filter, self._on_message)
            self._transport = t
        return self._transport

    def round_trip(self, *, tool_name, domain, parameters, timeout_s) -> dict:
        cid = str(uuid.uuid4())
        waiter: "queue.Queue[dict]" = queue.Queue(maxsize=1)
        with self._waiters_lock:
            self._waiters[cid] = waiter
        try:
            transport = self._ensure_started()
            transport.publish(
                f"stores/{self.store_id}/commands/{domain}",
                json.dumps({"correlation_id": cid, "tool_name": tool_name,
                            "parameters": parameters}).encode("utf-8"),
            )
            try:
                return waiter.get(timeout=timeout_s)
            except queue.Empty:
                # Reap the partial transfer so it does not leak, and report loud.
                # REVIEW (cravinho, 2026-08-18): sweep_expired(now=inf) expires EVERY
                # pending transfer, not just this cid's -- so a timeout on one round-trip
                # reaps and discards the buffers of all other in-flight transfers on this
                # connection (their errors are generated here and thrown away, and their
                # own waiters then time out with no buffered chunks). Fine for the
                # single-threaded experiment; in the pooled production analog this
                # connection IS shared across concurrent tool calls. Reap only `cid` (or
                # add a per-cid reap helper).
                for r in self._reassembler.sweep_expired(now=float("inf")):
                    if r.correlation_id == cid:
                        return {"status": "error", "error": r.error}
                return {"status": "error",
                        "error": {"code": "timeout",
                                  "message": f"The {tool_name} tool did not respond in time."}}
        finally:
            with self._waiters_lock:
                self._waiters.pop(cid, None)
