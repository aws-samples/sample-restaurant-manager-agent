# Chunking Large Tool Results over AWS IoT Core

This document specifies the design for returning tool results that exceed the AWS IoT Core
**128 KB** MQTT message limit, for the outbound-only store architecture described in
[`reference-architecture.md`](reference-architecture.md). It is the detailed companion to that
document's [Handling large tool results](reference-architecture.md#handling-large-tool-results-the-128-kb-limit)
section.

## Scope and constraints

The design is shaped by four hard facts about this environment:

1. **Direction is store→cloud.** The store gateway is the producer; the cloud executor is the
   consumer. Large results flow out of the store over the one connection it already holds.
2. **Outbound-only, single connection.** The store keeps one outbound mutually-authenticated
   (mTLS) MQTT connection to AWS IoT Core — no inbound ports, no second egress path, no AWS
   credentials at the store. Large results must therefore travel over the connection that
   already exists; the store simply sends more messages on it.
3. **AWS IoT Core reassembles nothing.** Unlike broker-native file transfer (e.g. EMQX
   Enterprise), IoT Core provides no ordering guarantee, only QoS 0/1 (no exactly-once), and an
   untrustworthy `DUP` flag. Every reliability property must be implemented in the cloud
   executor. This is **greenfield reassembly code**, not a liftable AWS primitive.
4. **Reassembly wants a stateful consumer.** A transfer's chunks must be buffered in one place
   until the set is complete. This fits the **in-process response subscriber** (the in-process round-tripon A):
   one long-lived MQTT subscription receives every chunk for the store, and the reassembler
   holds the per-`correlation_id` buffer in memory — no per-chunk function invocation, and one
   owner of the buffer means no cross-writer races.

The design emulates the EMQX file-transfer *shape* (stable transfer id → segments → finish
marker with checksum) while owning the reliability layer that a broker-native feature would
otherwise provide.

## Design principles

- **Compress and aggregate first; chunk only as a bounded fallback.** Most results should never
  reach this path — aggregate at the MCP source and gzip the payload. Chunking is for the
  small-but-over and low-MB band that survives both.
- **Content-addressed, idempotent reassembly.** Correctness never depends on arrival order or
  the `DUP` flag. Dedup is keyed on `correlation_id + seq`.
- **The store stays dumb.** It splits and publishes; all hard logic lives in the cloud executor
  we own and test.
- **Fail loud, never silent.** An incomplete or corrupted set returns a structured error the
  model can relay — never a hung request or a silently-truncated answer.

## Pipeline

```
tool result
   │
   ▼
[1] aggregate/compute at MCP source   → answer-shaped subset, not raw rows
   │
   ▼
[2] gzip (if compressible)            → text/JSON drops 5–10×; images unchanged
   │
   ▼
[3] measure encoded size
   ├── ≤ ~100 KB  → single message (total_chunks = 1)  ── done
   └── > budget   → chunk  ──►  [4] split + publish  ──►  [5] cloud reassembly
```

Only step 4–5 is the chunking mechanism; steps 1–3 are what keep the overwhelming majority of
results out of it.

## Wire format

Chunks are published to the **existing** correlation topic — multiple messages on the same
topic, so per-store IoT policy scoping is unchanged (no new topic per transfer):

```
stores/{store_id}/responses/{correlation_id}
```

Every response carries one JSON envelope. A single-message result is simply `total_chunks: 1`,
so the executor has a single code path:

```json
{
  "correlation_id": "c-4471",
  "part":     { "seq": 3, "total_chunks": 27 },
  "encoding": "gzip",              // or "identity"
  "checksum": "sha256:9f8e…",      // digest of the FULL reassembled payload (post-concat, pre-decompress)
  "final":    false,               // true only on the highest seq
  "data":     "…base64 chunk bytes…"
}
```

| Field | Purpose |
|---|---|
| `correlation_id` | Groups all segments of one logical result (the stable transfer id). |
| `part.seq` | 0-based segment index — the sole basis for ordering. |
| `part.total_chunks` | Expected segment count — completeness check. |
| `encoding` | `gzip` or `identity`; applied to the whole payload **before** splitting. |
| `checksum` | SHA-256 of the full reassembled bytes, **repeated on every chunk** so the executor knows the expected digest before the final segment arrives and can reject corrupt/duplicated data. |
| `final` | Belt-and-suspenders completeness signal alongside `total_chunks`. |
| `data` | Base64 chunk bytes. |

## Store side

```
result = tool.execute()
body   = gzip(serialize(result))            # compress first (skip for already-compressed blobs)
digest = sha256(body)

if len(envelope_with(body)) <= BUDGET:      # BUDGET ~100 KB, leaves room for envelope overhead
    publish(seq=0, total_chunks=1, final=True, checksum=digest, data=body)
else:
    chunks = split(body, CHUNK_SIZE)        # ~90 KB raw per chunk; base64 inflates ~33%,
    if len(chunks) > MAX_CHUNKS:            #   so the encoded message clears 128 KB
        publish_error("result too large to stream")   # explicit ceiling, not silent truncation
    else:
        for i, c in enumerate(chunks):
            publish(seq=i, total_chunks=len(chunks), final=(i == len(chunks)-1),
                    checksum=digest, encoding="gzip", data=c)   # QoS 1, same response topic
```

Notes:
- **Compress then split** — never split then compress.
- The store **fires all chunks back-to-back**; it does not wait for the cloud between segments.
  End-to-end latency ≈ one round-trip + transmission time.
- **Skip compression for already-compressed payloads** (JPEG/PNG batches, audio) —
  content-sniff or size-check first; gzip on those burns CPU on modest store hardware (NUC /
  Raspberry Pi) for ~0% gain.

## Cloud executor (reassembler)

Reassembly runs in the **long-lived in-process response subscriber**: the same
pooled MQTT subscription that already routes single-message responses receives every chunk for
the store, and the reassembler holds a per-`correlation_id` buffer in memory until the transfer
completes. A single owner of each buffer means there are no cross-process writer races to guard
against. For each arriving chunk:

```
1. Look up or create the buffer for correlation_id.
2. Idempotent insert: store data at index `seq`.
   If seq already present, DROP it.  ← QoS-1 dedup, keyed on correlation_id + seq, NEVER the DUP flag.
3. Record total_chunks and expected checksum from the header.
4. Completeness: all seq in [0, total_chunks) present AND `final` seen?
     - No  → wait.
     - Yes → concat by seq → verify sha256 == expected:
         · match    → gunzip if encoding == "gzip" → return ONE tool_result to the harness.
         · mismatch → fail loud: structured error ("result corrupted in transit").
```

Two guards make this safe under IoT Core's weak guarantees:

- **Dedup on `correlation_id + seq`.** QoS-1 redeliveries may arrive with a fresh packet id and
  no `DUP` flag, so the `DUP` flag cannot be trusted. Content-addressed dedup is the only
  correct approach.
- **Message-expiry + executor timeout.** Set MQTT message-expiry on the chunks and an
  application timeout on the buffer. If the set is still incomplete when the timer fires, return
  a structured error the model relays ("partial result: N of M segments") — mirroring the
  bounded-timeout contract the in-process round-trip already uses. Never hang; never return a truncated set
  as if complete.

## Bounds

State the limits explicitly rather than discovering them in production:

- **Cap `total_chunks`** (e.g. ≤ 64 → ~5.7 MB compressed). Beyond the cap the store returns a
  structured "result too large to stream" error rather than attempting an unbounded transfer.
  This is the explicit ceiling of the design.
- **Bounded throughput, not a firehose.** Fine for occasional MB-range results. A store that
  *routinely* needs multi-MB transfers is a signal to push harder on aggregation and
  compression upstream, so those results rarely reach this path.

## How this design answers the reliability objections

| IoT Core limitation | This design's answer |
|---|---|
| No message ordering | Reassemble by `seq`, not arrival order |
| Duplicates; untrustworthy `DUP` flag | Idempotent insert keyed on `correlation_id + seq` |
| No integrity guarantee | SHA-256 of full payload, verified before returning |
| Per-chunk compute blow-up | Reassemble in the long-lived in-process subscriber — no function invocation per chunk |
| Silent partial results | Message-expiry + executor timeout → loud structured error |
| Complexity has to live somewhere | Store stays dumb; reassembler is in the cloud we own and test |
| No AWS-native chunking primitive | Emulate EMQX's id/segment/finish + checksum shape; reassembly is our code |

## Keeping this path rare

Chunking is the fallback, not the default. Its cost and reliability burden **scales with payload
size** — more segments mean more reassembly state, more per-message metering, and a longer
tail-latency window — so the design leans on the two upstream steps to keep large results
uncommon: **aggregate at the MCP source** (return the answer, not the raw rows) and **compress**
(gzip shrinks JSON exports and time-series 5–10×, dropping most of what remains under the
128 KB limit). If a store *routinely* produces multi-MB irreducible results, that is the signal
to push harder on aggregation and compression upstream rather than to widen the chunking path.

## References

- AWS IoT Core service quotas (128 KB payload, non-adjustable; 5 KB metering):
  <https://docs.aws.amazon.com/general/latest/gr/iot-core.html>
- AWS IoT Core pricing (per-5 KB message metering):
  <https://aws.amazon.com/iot-core/pricing/>
- MQTT behavior in AWS IoT Core (QoS 0/1, DUP flag, correlation data, message expiry):
  <https://docs.aws.amazon.com/iot/latest/developerguide/mqtt.html>
- EMQX File Transfer over MQTT (reference design shape — id/segment/finish + SHA-256):
  <https://www.emqx.com/en/blog/file-transfer-over-mqtt>
- Well-Architected IoT Lens (reduce data transmitted; compression/binary encoding):
  <https://docs.aws.amazon.com/wellarchitected/latest/iot-lens/reduce-the-amount-of-data-transmitted.html>
