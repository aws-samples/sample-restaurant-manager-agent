# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Demo — prints the empirical numbers behind the chunking design.

Shows, for a realistic large tool result:
  * how much gzip shrinks it (why compression is the first line of defense),
  * how many 128 KB-budget chunks the compressed payload needs,
  * the per-5 KB metered message units (why chunking stays a rare fallback),
  * a full producer -> fault-injected stream -> reassembler round-trip.

Run: ../.venv/bin/python demo.py   (no broker, no AWS needed)
"""

from __future__ import annotations

import json

from chunking import (
    DEFAULT_CHUNK_BYTES,
    FaultInjector,
    Reassembler,
    chunk_result,
    encode_body,
    metered_units,
)


def _day_of_orders(n: int = 1500) -> dict:
    return {
        "status": "success",
        "tool_name": "get_transaction_history",
        "result": {
            "orders": [
                {"order_id": i, "ticket": round(8 + (i % 137) * 0.51, 2),
                 "server": f"server_{i % 18}", "items": (i % 9) + 1,
                 "voids": i % 23 == 0, "ts": f"2026-07-21T{(i % 24):02d}:{(i % 60):02d}:{(i % 60):02d}Z"}
                for i in range(n)
            ]
        },
    }


def main() -> None:
    result = _day_of_orders(1500)
    raw = json.dumps(result, separators=(",", ":")).encode("utf-8")
    body, encoding = encode_body(result, compress=True)

    print("=== Large tool result: a day of raw POS transactions (1,500 orders) ===")
    print(f"  raw JSON            : {len(raw):>10,} bytes")
    print(f"  gzip ({encoding})        : {len(body):>10,} bytes  "
          f"({len(raw) / max(len(body), 1):.1f}x smaller)")
    print(f"  IoT Core msg limit  : {128 * 1024:>10,} bytes")
    fits = "yes" if len(body) <= 100 * 1024 else "no"
    print(f"  fits in one message?: {fits:>10}")
    print()

    # Force a multi-chunk split to show the mechanism regardless of compression.
    chunks = chunk_result(result, "demo-cid", compress=False, chunk_bytes=DEFAULT_CHUNK_BYTES)
    print(f"=== Chunking the *uncompressed* {len(raw):,}-byte payload ===")
    print(f"  chunks ({DEFAULT_CHUNK_BYTES // 1024} KB each): {len(chunks)}")
    print(f"  metered units (per 5 KB) : {metered_units(chunks)}  "
          f"(~{metered_units(chunks) / len(chunks):.0f} per chunk)")
    print("  -> each chunk bills as ~26 units, not 1 -> keep chunking a rare fallback.")
    print()

    print("=== Round-trip through a shuffled + duplicated stream ===")
    fi = FaultInjector(seed=42)
    noisy = fi.shuffle(fi.duplicate(chunks, times=10))
    print(f"  produced {len(chunks)} chunks, delivered {len(noisy)} "
          f"(shuffled, +10 duplicates)")
    re = Reassembler()
    outcome = None
    for env in noisy:
        r = re.offer(env)
        if r.status in ("complete", "error"):
            outcome = r  # capture the first terminal outcome; later dupes are ignored
            break
    assert outcome is not None and outcome.status == "complete"
    print(f"  reassembled status : {outcome.status}")
    print(f"  payload intact     : {outcome.result == result}")


if __name__ == "__main__":
    main()
