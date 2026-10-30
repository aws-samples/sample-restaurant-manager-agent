# ADR-004: Migrate the MQTT transport from Amazon MQ to AWS IoT Core

## Status

**Accepted** — 2026-06-18. The 019 IoT Core spike
returned **GO** (all six properties passed live):
the 019 spike verdict (2026-06-17).
The connection-less design is validated end-to-end — the simulator connects over mTLS
with no code change, `iot-data:Publish` replaces the gateway MQTT client, and an IoT
Topic Rule → Lambda fully replaces the durable `mqtt-response-subscriber` (resumed a real
durable callback with live store data in 4.7s). Per-device policy isolation, offline
reconnect queuing, payload size, and latency all passed. Migration tracked by
spec 020.

**History**:
- 2026-06-16: Proposed (exploration ADR).
- 2026-06-18: Accepted after the 019 spike GO verdict. Migration spec 020 authored.

## Context

The store↔cloud tool-invocation transport currently runs on **Amazon MQ (ActiveMQ)**,
provisioned by spec 005. The store opens an
outbound TLS connection (username/password, port 8883), subscribes to its command
topics, and publishes responses; cloud agents reach the store only through this broker.
The secure-remote-tool-invocation model (see the project README) is the
project's core invariant: **the store accepts no inbound connections.**

Two motivations converge on revisiting the transport:

1. **Realism / device identity (primary).** A real restaurant deployment would not
   authenticate a store with a shared broker username/password. It would use a
   **per-device X.509 certificate** — the IoT Core model. Migrating makes the simulator
   faithful to how a production fleet of stores would actually connect and be authorized,
   which is the whole point of the simulator and the companion blog.
2. **Cloud-side simplification (enabled by the migration).** Amazon MQ forces two
   *persistent MQTT connections* in the cloud: the gateway Lambdas hold a broker client
   to publish, and the durable `mqtt-response-subscriber` (spec 011)
   holds a long-lived subscription to resume durable callbacks. Both are awkward fits for
   serverless. IoT Core removes the need for either (see Decision).

### What the codebase already supports

- **Store side is mTLS-ready.** `simulator/mqtt/config.py` already implements
  `MQTT_AUTH_MODE=mtls` with `client_cert_path` / `client_key_path` / `ca_bundle_path`
  and full validation. The bridge was built broker-agnostic. Migrating the simulator is
  expected to be **config + cert delivery, not code.**
- **Cloud side assumes Amazon MQ userpass.** The gateway Lambdas (`infra/cloud-mcp-gateway`)
  and the durable subscriber connect with username/password from a Secrets Manager broker
  secret. These are the components that change.

## Decision (proposed)

**Migrate the transport to AWS IoT Core and redesign the cloud response path to be
connection-less, retiring the `mqtt-response-subscriber`.** This is the "ambitious"
scope (not a like-for-like swap). Specifically:

1. **Store identity → X.509.** Provision a per-store IoT thing + certificate + a scoped
   IoT policy that restricts each store to its own topic space
   (`stores/${iot:Connection.Thing.ThingName}/...`). The simulator connects via mTLS —
   a `.env`/cert change, no code.
2. **Publish path → `iot-data` API.** Gateway Lambdas publish commands with a boto3
   `iot-data:Publish` call instead of holding an MQTT client. This removes the persistent
   broker connection *and* the Secrets Manager broker-credentials dependency from the
   gateway Lambdas.
3. **Response path → IoT Rule → Lambda.** An IoT Topic Rule on
   `stores/+/responses/+` triggers a small Lambda that does exactly what the durable
   subscriber does today: look up `callback_id` in `CommandResults` and call
   `SendDurableExecutionCallbackSuccess` (plus the `StoreStateCache` write). This
   **retires the `mqtt-response-subscriber`** and its long-lived MQTT session entirely.
4. **Retire Amazon MQ.** Replace spec 005's broker stack with an IoT Core stack. Decommission
   the broker (a spec-016-style takedown) once the new path is validated in production.
5. **Keep unchanged**: the topic layout (`stores/{id}/commands|responses/...`), the
   correlation-id + `CommandResults` model, the dedup/idempotency guarantees, the durable
   agent layer (ADR-001), and the two-tier structure (ADR-002).

The security invariant is **preserved and arguably strengthened**: the store still only
opens an outbound mTLS connection to the IoT Core endpoint (no inbound), and per-device
IoT policy gives finer-grained authorization than a shared broker login.

```
        BEFORE (Amazon MQ)                         AFTER (IoT Core)
 gateway Lambda --MQTT client--> MQ        gateway Lambda --iot-data:Publish--> IoT Core
 store <--outbound TLS--> MQ                store <--outbound mTLS (X.509)--> IoT Core
 store publishes response --> MQ           store publishes response --> IoT Core
 MQ --> mqtt-response-subscriber (durable, IoT Rule (stores/+/responses/+)
   long-lived MQTT sub) --> callback           --> response Lambda --> callback
```

## Consequences (proposed)

### Positive
- **Faithful device identity** — per-store X.509 cert + scoped IoT policy mirrors a real
  fleet; directly serves the simulator's purpose and the blog narrative.
- **No persistent MQTT connections in the cloud** — gateway Lambdas publish via an API
  call; the response path is event-driven via an IoT Rule. Removes the durable
  `mqtt-response-subscriber`, its long-lived session management, and the
  broker-credentials Secrets Manager dependency.
- **Scale-to-zero transport** — no always-on `mq.t3.micro` broker instance; IoT Core is
  pay-per-message/connection. Aligns with the project's serverless posture (same spirit
  as ADR-001).
- **Finer-grained authz** — per-device topic policy beats a shared broker username.
- **Retires a class of durable gotchas** — the spec 011 subscriber's MQTT-session
  findings (broker reconnect, `rc=7` disconnects, session-epoch handling) stop mattering.

### Negative
- **X.509 lifecycle is new operational surface** — provisioning, rotation, and revocation
  of per-store certs. For the single simulator store this is trivial; documenting the
  path to fleet provisioning (JITP / fleet provisioning) is part of the realism story but
  is added scope.
- **IoT Core constraints** — 128 KB max MQTT payload (vs Amazon MQ's larger limits; verify
  the largest tool response fits), QoS 0/1 only (current design uses QoS 1 — fine),
  per-connection/message quotas. Persistent-session and retained-message semantics differ
  from ActiveMQ and must be re-validated against the current `clean_session=False`
  reconnect-buffering behavior.
- **New moving part: IoT Rules Engine** — a rule SQL + a response Lambda replace the
  subscriber. Net simpler, but a different failure mode to learn (rule match failures,
  Lambda action retries).
- **Invalidates spec 005** and requires a spec-016-style Amazon MQ decommission once
  validated.
- **Migration churn** — gateway Lambdas rewired to `iot-data:Publish`; new response
  Lambda; new IoT Core CDK stack; simulator cert delivery.

### Neutral
- Durable agent layer (ADR-001) and two-tier structure (ADR-002) are unaffected.
- `CommandResults` / `StoreStateCache` keep their roles; the response Lambda uses them
  exactly as the subscriber did.
- Topic layout and correlation/dedup model unchanged.

## Alternatives Considered

- **A. Keep Amazon MQ (do nothing).** Works today. Rejected as the forward direction
  because it doesn't deliver the device-identity realism (the primary motivation) and
  keeps two persistent cloud MQTT connections. Remains the fallback if the spike finds
  IoT Core unsuitable.
- **B. Like-for-like swap** — IoT Core transport but keep a persistent MQTT subscriber.
  Rejected: leaves the long-lived-connection awkwardness on the table and misses the main
  cloud simplification (per user direction 2=b).
- **C. Dual-broker (Amazon MQ + IoT Core side by side).** Rejected: doubles the transport
  surface and defers the decision rather than making it.

## Open Questions (for a follow-up validation spike)

A tightly-scoped spike — mirroring the discipline of the 009/010 and 018 spikes — should
close these before this ADR is Accepted:

1. **mTLS from the simulator to IoT Core.** Does the existing `mtls` config path connect,
   subscribe, and publish against an IoT Core endpoint unchanged? (Expected yes; prove it.)
2. **IoT Rule → response Lambda → callback resume.** Can an IoT Topic Rule on
   `stores/+/responses/+` reliably trigger a Lambda that calls
   `SendDurableExecutionCallbackSuccess`, fully replacing the subscriber? Measure latency
   vs the current subscriber path.
3. **Gateway publish via `iot-data:Publish`.** Confirm a Lambda can publish commands with
   the boto3 IoT data plane (no MQTT client) and the store receives them.
4. **Payload size & QoS.** Confirm the largest real tool response (e.g. full equipment +
   temperature log) fits under 128 KB and QoS 1 behaves as needed.
5. **Persistent session / reconnect.** Validate IoT Core session behavior against the
   simulator's `clean_session=False` reconnect-buffering assumptions, or adjust them.
6. **Per-device IoT policy.** Verify a `${iot:Connection.Thing.ThingName}`-scoped policy
   enforces topic isolation without breaking the command/response/dead-letter topics.

## Relationship to prior decisions

- **ADR-001**: This realizes its deferred "Alternative 4." The durable agent layer stays;
  only the transport and the response-resolution mechanism change. The
  `mqtt-response-subscriber` (spec 011) is retired, and its MQTT-session steering gotchas
  become historical.
- **ADR-002**: Unaffected.
- **ADR-003**: Independent. (Note: ADR-003's spike already proved the harness problem was
  *not* transport-related, so this migration neither helps nor revisits it.)

## References

- [ADR-001](ADR-001-durable-lambda-for-agents.md) "Alternative 4" — the deferred broker swap.
- spec 005 — the Amazon MQ stack this would replace.
- spec 011 — the subscriber this would retire.
- `simulator/mqtt/config.py` — already-present `mtls` auth mode.
- The secure remote tool invocation pattern (see the project README) - the security invariant (preserved).

Note: external documentation content referenced here has been summarized and rephrased
for compliance with licensing restrictions.
