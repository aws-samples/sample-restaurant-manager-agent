# Large-payload chunking experiment

A local, runnable experiment that builds and tests the chunking solution from
[`docs/solution-guidance/large-payload-chunking-design.md`](../docs/solution-guidance/large-payload-chunking-design.md):
returning tool results larger than AWS IoT Core's **128 KB** MQTT message limit
by compressing, splitting into ordered segments, and reassembling them
cloud-side.

It validates the **logic and wiring** — the risky part — without needing store
hardware or an AWS account. Most of it needs no broker at all.

## What's here

| File | What it is |
|---|---|
| `chunking.py` | Reference implementation: producer (`chunk_result`), reassembler (`Reassembler`), seeded `FaultInjector`, and a `metered_units` cost counter. No AWS/MQTT imports — pure logic. |
| `test_chunking.py` | **Slice 1** — adversarial unit tests: out-of-order, duplicate, missing, corrupt, over-cap, interleaved transfers, gzip round-trip, metering. No broker. |
| `executor.py` | **Slice 2** — a chunk-aware Correlation-A executor mirroring `simulator/api/harness_tools.py::_ToolConnection`; reassembly slots into `_on_message`. |
| `test_executor.py` | **Slice 2** tests — driven by the repo's injected-transport (`_FakeTransport`) pattern; full round-trip, no broker. |
| `docker-compose.yml`, `mosquitto.conf` | **Slice 3** — a local Mosquitto broker (stand-in for IoT Core's MQTT). |
| `test_e2e_mosquitto.py` | **Slice 3** — producer → real broker (QoS 1) → subscriber → reassembler. Auto-skips if paho or a broker is absent. |
| `demo.py` | Prints the empirical numbers (compression ratio, chunk count, metered units, a fault-injected round-trip). |

## Running it

From the repo root, using the project virtualenv (`.venv`):

```bash
# one-time: deps
python3 -m venv .venv
.venv/bin/pip install pytest pytest-asyncio paho-mqtt

# Slices 1 + 2 (no broker needed)
cd large-payload-experiment
../.venv/bin/python -m pytest test_chunking.py test_executor.py -v

# See the numbers
../.venv/bin/python demo.py

# Slice 3: real MQTT over local Mosquitto
docker compose -f docker-compose.yml up -d
../.venv/bin/python -m pytest test_e2e_mosquitto.py -v
docker compose -f docker-compose.yml down
```

Run everything at once (Slice 3 auto-skips if no broker is up):

```bash
../.venv/bin/python -m pytest large-payload-experiment/ -v
```

## What it proves — and what it can't

**Proves:** reassembly correctness under reordering, duplication (QoS-1
redelivery), and loss; content-addressed dedup on `(correlation_id, seq)`;
SHA-256 integrity rejection of corrupt payloads; fail-loud timeout on
incomplete sets; gzip round-trip; single-message and multi-chunk on one code
path; no cross-contamination between interleaved transfers; and the topic /
envelope contract over a real MQTT broker at QoS 1.

**Can't faithfully reproduce** (out of scope for a logic test): IoT Core's
actual 128 KB rejection, the per-5 KB billing (simulated as a counter), mTLS
device identity, and real store-network latency or packet loss (the seeded
`FaultInjector` approximates the delivery faults).

## A result worth noting

`demo.py` on a day of raw POS transactions (1,500 orders): the ~157 KB JSON
**gzips ~10× to ~16 KB and fits in a single MQTT message.** That is the design's
whole point — compression and source-side aggregation keep the overwhelming
majority of results under the limit, so chunking stays a rare, bounded fallback.

## Where this maps in production

- **Producer** → `simulator/mqtt/dispatcher.py` (`_respond_success`) →
  `simulator/mqtt/publisher.py` (`ResponsePublisher.publish`): compress →
  checksum → split → publish each chunk QoS 1 on the same
  `stores/{id}/responses/{cid}` topic.
- **Reassembler** → `simulator/api/harness_tools.py::_ToolConnection._on_message`:
  buffer by `seq`, dedup on `correlation_id + seq`, verify checksum, gunzip,
  fulfil the waiter once complete. `executor.py` here is a faithful sketch of
  that change.
