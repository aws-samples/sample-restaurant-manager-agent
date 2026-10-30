// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Flow definitions for the architecture visualization -- pure data, no React.
 *
 * Each flow is a hand-authored script: nodes with layout coordinates, group
 * containers, and an ordered list of steps. Because coordinates live here rather
 * than being measured from the DOM, edge geometry and packet positions are plain
 * arithmetic (see `geometry.js`) and the whole thing is unit-testable.
 *
 * Every flow carries a `docRef` naming the artifact it was transcribed from, so
 * drift between this page and the published figures is traceable (AC 2.6).
 *
 * Layout conventions:
 *   - `viewBox` is the SVG user-space box; all coordinates are in that space.
 *   - A node's `group` must resolve to a group id, or be `null` for an actor
 *     that sits outside any container (e.g. the manager).
 *   - Node boxes must fall inside their group's bounds -- asserted by
 *     `__tests__/flows.test.js`, which turns a drifted box into a test failure
 *     rather than something noticed by eye (or not).
 *
 * Note on antiparallel edges: several flows send a request and its response
 * between the same two boxes (e.g. steps 2/3 and 5/7 below). Those connectors
 * share a path; only the active step is drawn at full strength, which is what
 * keeps them readable.
 */

/**
 * Flow 1 -- the tool round-trip (the in-process round-trip; 'Correlation A' in
 * older specs/findings), the current production path.
 * Transcribed step-for-step from the diagram source named in `docRef`; step
 * kinds follow the requirements table (spec 030 § Flow 1).
 */
const toolRoundTrip = {
  id: 'tool-round-trip',
  label: 'Tool round-trip',
  subtitle: 'The in-process round-trip — the current production path',
  docRef: 'docs/solution-guidance/diagrams/diagram3_tool_invocation_flow.py',
  viewBox: { w: 1200, h: 560 },
  groups: [
    { id: 'cloud', kind: 'cloud', label: 'AWS Cloud', x: 240, y: 56, w: 548, h: 448 },
    {
      id: 'store',
      kind: 'onprem',
      label: 'Store — behind the firewall · outbound-only',
      x: 828,
      y: 56,
      w: 332,
      h: 448,
    },
  ],
  nodes: [
    {
      id: 'manager',
      group: null,
      label: 'Restaurant',
      sublabel: 'general manager',
      x: 40,
      y: 238,
      w: 160,
      h: 84,
    },
    {
      id: 'chat',
      group: 'cloud',
      label: 'Chat backend',
      sublabel: 'inline-tool executor',
      x: 276,
      y: 238,
      w: 200,
      h: 96,
    },
    {
      id: 'harness',
      group: 'cloud',
      label: 'AgentCore Managed Harness',
      sublabel: 'Amazon Bedrock',
      x: 548,
      y: 92,
      w: 208,
      h: 96,
    },
    {
      id: 'iot',
      group: 'cloud',
      label: 'AWS IoT Core',
      sublabel: 'MQTT broker',
      x: 548,
      y: 384,
      w: 208,
      h: 96,
    },
    {
      id: 'tool',
      group: 'store',
      label: 'MCP tool',
      sublabel: '+ store system',
      x: 864,
      y: 180,
      w: 260,
      h: 96,
    },
    {
      id: 'gateway',
      group: 'store',
      label: 'Store gateway',
      sublabel: 'outbound mTLS only',
      x: 864,
      y: 384,
      w: 260,
      h: 96,
    },
  ],
  steps: [
    {
      n: 1,
      from: 'manager',
      to: 'chat',
      kind: 'request',
      label: 'ask (chat)',
      caption:
        'The manager asks an operational question. POST /api/chat returns 202 Accepted with an executionId; the client polls for the answer.',
      durationMs: 1800,
    },
    {
      n: 2,
      from: 'chat',
      to: 'harness',
      kind: 'request',
      label: 'invoke_harness',
      caption:
        'The backend invokes the AgentCore Managed Harness for this session, passing the message and the inline tool catalog. The harness owns the reasoning loop.',
      durationMs: 2000,
    },
    {
      n: 3,
      from: 'harness',
      to: 'chat',
      kind: 'response',
      label: 'tool_use (get_equipment_status)',
      caption:
        'The model decides it needs equipment data and streams a tool_use block, stopping with reason "tool_use". Tool selection is the harness\'s job, not the backend\'s.',
      durationMs: 2200,
    },
    {
      n: 4,
      from: 'chat',
      to: 'iot',
      kind: 'request',
      label: 'iot-data:Publish command + SigV4 subscribe response',
      caption:
        'The inline-tool executor first subscribes to the per-correlation response topic over MQTT-over-WebSocket (SigV4), then publishes the command to stores/{id}/commands/{domain} at QoS 1 via the connection-less iot-data plane.',
      durationMs: 2600,
    },
    {
      n: 5,
      from: 'iot',
      to: 'gateway',
      kind: 'outbound',
      label: "deliver over the store's outbound mTLS connection",
      caption:
        'IoT Core delivers the command down the connection the STORE opened. This is the load-bearing property of the pattern: no inbound port, no VPN, no DMZ at the store.',
      durationMs: 3000,
    },
    {
      n: 6,
      from: 'gateway',
      to: 'tool',
      kind: 'request',
      label: 'dispatch + read/act',
      caption:
        'The gateway checks its correlation-id dedup cache (so a QoS-1 redelivery replays instead of re-executing a write), then dispatches to the MCP server, which reads the real store system.',
      durationMs: 2400,
    },
    {
      n: 7,
      from: 'gateway',
      to: 'iot',
      kind: 'response',
      label: 'publish response',
      caption:
        'The store publishes the result to stores/{id}/responses/{correlation_id} — back out over the same outbound connection.',
      durationMs: 2200,
    },
    {
      n: 8,
      from: 'iot',
      to: 'chat',
      kind: 'response',
      label: 'response (bounded 30s wait)',
      caption:
        'The executor\'s subscription receives the response within a bounded timeout, sanitizes it, and returns it as a tool result. On timeout it returns a structured error the model can reason about instead of hanging.',
      durationMs: 2400,
    },
    {
      n: 9,
      from: 'chat',
      to: 'harness',
      kind: 'request',
      label: 're-invoke (toolResult) → final answer',
      caption:
        'The backend re-invokes the harness with BOTH the assistant toolUse message and the user toolResult message — the harness does not persist the partial turn — and the model composes the final answer.',
      durationMs: 2800,
      bidirectional: true,
    },
    {
      n: 10,
      from: 'chat',
      to: 'manager',
      kind: 'response',
      label: 'answer',
      caption:
        'The next poll of /api/chat/result returns "succeeded" with a grounded answer built from live store data — e.g. "The walk-in cooler is at 38°F, within normal range."',
      durationMs: 2000,
    },
  ],
}

/**
 * Flow 2 -- agent-initiated proactive alerting (spec 023 / ADR-006).
 *
 * Transcribed from the diagram source in `docRef`. One intentional divergence:
 * diagram 4 renders `ingest >> eb >> sfn` as a single chained edge; this splits
 * them into steps 4 and 5 so each hop gets its own caption.
 */
const proactiveAlerting = {
  id: 'proactive-alerting',
  label: 'Proactive alerting',
  subtitle: 'Agent-initiated — the store speaks first',
  docRef: 'docs/solution-guidance/diagrams/diagram4_proactive_alerting.py',
  viewBox: { w: 1200, h: 620 },
  groups: [
    {
      id: 'store',
      kind: 'onprem',
      label: 'On-premises store',
      x: 40,
      y: 60,
      w: 290,
      h: 500,
    },
    { id: 'cloud', kind: 'cloud', label: 'AWS Cloud', x: 380, y: 60, w: 780, h: 500 },
  ],
  nodes: [
    {
      id: 'engine',
      group: 'store',
      label: 'Store engine',
      sublabel: 'equipment · inventory',
      x: 70,
      y: 110,
      w: 230,
      h: 96,
    },
    {
      id: 'display',
      group: 'store',
      label: 'Always-on display',
      sublabel: 'inbox · WebSocket · voice',
      x: 70,
      y: 300,
      w: 230,
      h: 110,
    },
    {
      id: 'iot',
      group: 'cloud',
      label: 'AWS IoT Core',
      sublabel: 'topic rules',
      x: 420,
      y: 300,
      w: 200,
      h: 110,
    },
    {
      id: 'ingest',
      group: 'cloud',
      label: 'Ingest Lambda',
      sublabel: 'validate · dedup · persist',
      x: 680,
      y: 90,
      w: 220,
      h: 96,
    },
    {
      id: 'alerts',
      group: 'cloud',
      label: 'Alerts table',
      sublabel: 'DynamoDB · TTL',
      x: 960,
      y: 90,
      w: 180,
      h: 96,
    },
    {
      id: 'eventbridge',
      group: 'cloud',
      label: 'EventBridge',
      sublabel: 'AlertRaised',
      x: 680,
      y: 250,
      w: 220,
      h: 96,
    },
    {
      id: 'policy',
      group: 'cloud',
      label: 'Step Functions alert policy',
      sublabel: 'severity · quiet hours · ack SLA',
      x: 680,
      y: 420,
      w: 250,
      h: 110,
    },
  ],
  steps: [
    {
      n: 1,
      from: 'engine',
      to: 'iot',
      kind: 'outbound',
      label: 'alert event stores/{id}/events/{domain}',
      caption:
        'A sensor or stock condition trips and the store publishes an alert envelope at QoS 1 — over the SAME outbound connection it already holds. Nothing inbound is opened to make the store speak first.',
      durationMs: 2800,
    },
    {
      n: 2,
      from: 'iot',
      to: 'ingest',
      kind: 'request',
      label: 'IoT Topic Rule',
      caption:
        'A topic rule on stores/+/events/+ invokes the ingest Lambda — the same rule-to-Lambda shape already proven for tool responses, so this adds no new connectivity model.',
      durationMs: 2200,
    },
    {
      n: 3,
      from: 'ingest',
      to: 'alerts',
      kind: 'request',
      label: 'dedup + persist',
      caption:
        'Ingest derives a time-bucketed key and does a single conditional PutItem. That one atomic write is the dedup gate: QoS-1 redelivery and a flapping sensor collapse to one row, with no read-then-write race. The store id comes from the authenticated topic, never the payload body.',
      durationMs: 2600,
    },
    {
      n: 4,
      from: 'ingest',
      to: 'eventbridge',
      kind: 'request',
      label: 'AlertRaised',
      caption:
        'Only a genuinely new alert emits AlertRaised, which decouples capture from policy — the ingest path does not need to know what happens next.',
      durationMs: 2000,
    },
    {
      n: 5,
      from: 'eventbridge',
      to: 'policy',
      kind: 'request',
      label: 'trigger alert policy',
      caption:
        'EventBridge starts the Step Functions workflow that owns alert policy: severity gating, quiet hours with a critical override, dedup window, and the acknowledgment SLA.',
      durationMs: 2000,
    },
    {
      n: 6,
      from: 'policy',
      to: 'iot',
      kind: 'request',
      label: 'notification stores/{id}/notifications/{id}',
      caption:
        'Warning and above notify. The workflow publishes the notification and then PAUSES on a task token, persisted onto the alert row, so it can wait minutes for a human without holding any compute.',
      durationMs: 2400,
    },
    {
      n: 7,
      from: 'iot',
      to: 'display',
      kind: 'outbound',
      label: 'deliver (outbound mTLS)',
      caption:
        'The notification arrives on the always-on display: an in-app inbox entry (the durable floor, so an alert is never silently lost), a live WebSocket push, and — with the voice edge — spoken aloud.',
      durationMs: 2800,
    },
    {
      n: 8,
      from: 'display',
      to: 'policy',
      kind: 'ack',
      label: 'acknowledge → resume (SendTaskSuccess)',
      caption:
        'Acknowledging looks the token up and calls SendTaskSuccess to resume the paused workflow. The ack is idempotent, and if nobody acknowledges within the SLA the alert re-surfaces on the same display a bounded number of times, then expires.',
      durationMs: 3000,
    },
  ],
}

/**
 * Flow 3 -- the Nova Sonic voice edge (ADR-005 / spec 025).
 *
 * Authored from the README voice section, ADR-005, and `simulator/voice/broker.py`.
 *
 * NOTE: there is deliberately **no AgentCore harness node here**. ADR-005 narrows
 * ADR-003 to "text and analytics": Nova Sonic owns its own tool-use on the voice
 * path and calls store tools directly over the same in-process round-trip. Do
 * not "fix" this by pattern-matching from the tool-round-trip flow.
 */
const voiceEdge = {
  id: 'voice-edge',
  label: 'Voice edge',
  subtitle: 'Amazon Nova Sonic — speech to speech on the in-store display',
  docRef: 'simulator/voice/broker.py + docs/adr/ADR-005-nova-sonic-voice-interface.md',
  viewBox: { w: 1200, h: 680 },
  groups: [
    {
      id: 'store',
      kind: 'onprem',
      label: 'On-premises store',
      x: 40,
      y: 60,
      w: 520,
      h: 560,
    },
    { id: 'cloud', kind: 'cloud', label: 'AWS Cloud', x: 620, y: 60, w: 540, h: 560 },
  ],
  nodes: [
    {
      id: 'browser',
      group: 'store',
      label: 'Browser display',
      sublabel: 'mic + speaker',
      x: 70,
      y: 110,
      w: 220,
      h: 100,
    },
    {
      id: 'broker',
      group: 'store',
      label: 'Voice broker',
      sublabel: 'server-side — holds the stream',
      x: 70,
      y: 280,
      w: 220,
      h: 110,
    },
    {
      id: 'tool',
      group: 'store',
      label: 'MCP tool',
      sublabel: '+ store system',
      x: 70,
      y: 470,
      w: 210,
      h: 100,
    },
    {
      id: 'gateway',
      group: 'store',
      label: 'Store gateway',
      sublabel: 'outbound mTLS only',
      x: 320,
      y: 470,
      w: 210,
      h: 100,
    },
    {
      id: 'sonic',
      group: 'cloud',
      label: 'Amazon Nova Sonic',
      sublabel: 'bidirectional stream',
      x: 660,
      y: 110,
      w: 240,
      h: 120,
    },
    {
      id: 'iot',
      group: 'cloud',
      label: 'AWS IoT Core',
      sublabel: 'MQTT broker',
      x: 660,
      y: 320,
      w: 240,
      h: 110,
    },
  ],
  steps: [
    {
      n: 1,
      from: 'browser',
      to: 'broker',
      kind: 'request',
      label: 'mic PCM over WebSocket',
      caption:
        'The browser streams microphone audio to a server-side broker over a WebSocket. It holds NO AWS credentials — that is the whole point of putting a broker in front of Bedrock.',
      durationMs: 2200,
    },
    {
      n: 2,
      from: 'broker',
      to: 'sonic',
      kind: 'request',
      label: 'audioInput on the bidirectional stream',
      caption:
        'The broker owns the Bedrock bidirectional stream and forwards audio frames as audioInput. Because the stream lives server-side, credentials never reach the browser.',
      durationMs: 2200,
    },
    {
      n: 3,
      from: 'sonic',
      to: 'broker',
      kind: 'response',
      label: 'toolUse (toolName, toolUseId)',
      caption:
        'Nova Sonic decides it needs store data and emits a toolUse on the stream. It owns its own tool-use here — there is no text-agent harness in this path. It is offered a curated 7-tool set, because real-time tool selection degrades when too many tools are presented.',
      durationMs: 2600,
    },
    {
      n: 4,
      from: 'broker',
      to: 'iot',
      kind: 'request',
      label: 'validate params → execute_tool (in-process round-trip)',
      caption:
        'The broker validates the parameters and dispatches through the SAME in-process round-trip the text path uses, so voice and text share one tool contract rather than drifting apart.',
      durationMs: 2400,
    },
    {
      n: 5,
      from: 'iot',
      to: 'gateway',
      kind: 'outbound',
      label: "deliver over the store's outbound mTLS connection",
      caption:
        'Same outbound-only delivery as the text path: the command rides the connection the store opened. Voice changes what talks to the model, not how the store is reached.',
      durationMs: 2800,
    },
    {
      n: 6,
      from: 'gateway',
      to: 'tool',
      kind: 'request',
      label: 'dispatch + read/act',
      caption:
        'The gateway dispatches to the MCP server, which reads or acts on the real store system — identical to the text path, including the correlation-id dedup cache.',
      durationMs: 2400,
    },
    {
      n: 7,
      from: 'iot',
      to: 'broker',
      kind: 'response',
      label: 'tool response (bounded wait)',
      caption:
        'The store publishes its result back to the per-correlation response topic and IoT Core returns it to the broker, which is waiting on a bounded timeout.',
      durationMs: 2400,
    },
    {
      n: 8,
      from: 'broker',
      to: 'sonic',
      kind: 'request',
      label: 'toolResult on the stream',
      caption:
        'The broker sends the result back as a toolResult on the open stream — no new session, no replayed history.',
      durationMs: 2000,
    },
    {
      n: 9,
      from: 'sonic',
      to: 'broker',
      kind: 'response',
      label: 'audioOutput (spoken answer)',
      caption:
        'Nova Sonic speaks the answer, streaming audio back as it generates rather than waiting for a complete response — which is what keeps the conversation feeling live.',
      durationMs: 2600,
    },
    {
      n: 10,
      from: 'broker',
      to: 'browser',
      kind: 'response',
      label: 'audio to speaker (barge-in capable)',
      caption:
        'The broker relays audio to the display speaker. The manager can interrupt mid-sentence (barge-in) and audio is never persisted — no recording, no transcript retention by default.',
      durationMs: 2400,
    },
  ],
}

/** All flows, in presentation order. */
export const FLOWS = [toolRoundTrip, proactiveAlerting, voiceEdge]

/** Look up a flow by id; returns `undefined` if unknown. */
export function flowById(id) {
  return FLOWS.find((f) => f.id === id)
}

/** The flow selected when the page first mounts. */
export const DEFAULT_FLOW_ID = toolRoundTrip.id
