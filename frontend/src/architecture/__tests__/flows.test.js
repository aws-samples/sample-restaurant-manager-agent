// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Flow-data schema tests (spec 030 AC 6.5).
 *
 * These convert a class of silent layout/authoring bug -- a typo'd node id, a
 * box that drifted outside its container, a duplicated step number -- into a
 * test failure instead of something spotted by eye, or not spotted at all.
 *
 * Parameterized over every flow in `FLOWS`, so flows added later (proactive
 * alerting, voice edge) are held to the same invariants automatically.
 */
import { describe, it, expect } from 'vitest'

import { FLOWS, DEFAULT_FLOW_ID, flowById } from '../flows.js'
import { GROUP_KINDS, STEP_KINDS } from '../theme.js'

/** True when the `inner` box lies entirely within the `outer` box. */
function contains(outer, inner) {
  return (
    inner.x >= outer.x &&
    inner.y >= outer.y &&
    inner.x + inner.w <= outer.x + outer.w &&
    inner.y + inner.h <= outer.y + outer.h
  )
}

describe('FLOWS registry', () => {
  it('is non-empty', () => {
    expect(FLOWS.length).toBeGreaterThan(0)
  })

  it('has unique flow ids', () => {
    const ids = FLOWS.map((f) => f.id)
    expect(new Set(ids).size).toBe(ids.length)
  })

  it('exposes a default flow that resolves', () => {
    expect(flowById(DEFAULT_FLOW_ID)).toBeDefined()
  })

  it('returns undefined for an unknown id', () => {
    expect(flowById('no-such-flow')).toBeUndefined()
  })
})

describe.each(FLOWS.map((f) => [f.id, f]))('flow "%s"', (_id, flow) => {
  it('declares identity and a doc source for drift tracing (AC 2.6)', () => {
    expect(flow.label).toBeTruthy()
    expect(flow.subtitle).toBeTruthy()
    expect(flow.docRef).toBeTruthy()
  })

  it('has a positive viewBox', () => {
    expect(flow.viewBox.w).toBeGreaterThan(0)
    expect(flow.viewBox.h).toBeGreaterThan(0)
  })

  // --- groups --------------------------------------------------------------

  it('has uniquely-identified groups with known kinds', () => {
    const ids = flow.groups.map((g) => g.id)
    expect(new Set(ids).size).toBe(ids.length)
    for (const g of flow.groups) {
      expect(GROUP_KINDS).toContain(g.kind)
      expect(g.label).toBeTruthy()
    }
  })

  it('keeps every group inside the viewBox', () => {
    const box = { x: 0, y: 0, w: flow.viewBox.w, h: flow.viewBox.h }
    for (const g of flow.groups) {
      expect(contains(box, g), `group "${g.id}" escapes the viewBox`).toBe(true)
    }
  })

  // --- nodes ---------------------------------------------------------------

  it('has uniquely-identified nodes with positive boxes', () => {
    const ids = flow.nodes.map((n) => n.id)
    expect(new Set(ids).size).toBe(ids.length)
    for (const n of flow.nodes) {
      expect(n.label, `node "${n.id}" needs a label`).toBeTruthy()
      expect(n.w).toBeGreaterThan(0)
      expect(n.h).toBeGreaterThan(0)
    }
  })

  it("resolves every node's group to a real group, or null for an actor (AC 6.5)", () => {
    const groupIds = new Set(flow.groups.map((g) => g.id))
    for (const n of flow.nodes) {
      if (n.group === null) continue
      expect(groupIds.has(n.group), `node "${n.id}" references unknown group "${n.group}"`).toBe(
        true,
      )
    }
  })

  it('keeps every node box inside its group bounds (AC 6.5)', () => {
    const byId = new Map(flow.groups.map((g) => [g.id, g]))
    for (const n of flow.nodes) {
      if (n.group === null) continue
      const g = byId.get(n.group)
      expect(contains(g, n), `node "${n.id}" escapes group "${g.id}"`).toBe(true)
    }
  })

  it('keeps ungrouped actor nodes inside the viewBox', () => {
    const box = { x: 0, y: 0, w: flow.viewBox.w, h: flow.viewBox.h }
    for (const n of flow.nodes) {
      expect(contains(box, n), `node "${n.id}" escapes the viewBox`).toBe(true)
    }
  })

  // --- steps ---------------------------------------------------------------

  it('numbers steps contiguously from 1 (AC 6.5)', () => {
    expect(flow.steps.length).toBeGreaterThan(0)
    expect(flow.steps.map((s) => s.n)).toEqual(flow.steps.map((_s, i) => i + 1))
  })

  it('resolves every step endpoint to a real node id (AC 6.5)', () => {
    const nodeIds = new Set(flow.nodes.map((n) => n.id))
    for (const s of flow.steps) {
      expect(nodeIds.has(s.from), `step ${s.n} "from" is unknown: "${s.from}"`).toBe(true)
      expect(nodeIds.has(s.to), `step ${s.n} "to" is unknown: "${s.to}"`).toBe(true)
      expect(s.from, `step ${s.n} connects a node to itself`).not.toBe(s.to)
    }
  })

  it('gives every step a positive duration (AC 6.5)', () => {
    for (const s of flow.steps) {
      expect(typeof s.durationMs).toBe('number')
      expect(s.durationMs, `step ${s.n} needs a positive durationMs`).toBeGreaterThan(0)
    }
  })

  it('gives every step a known kind, a label, and a caption', () => {
    for (const s of flow.steps) {
      expect(STEP_KINDS, `step ${s.n} has unknown kind "${s.kind}"`).toContain(s.kind)
      expect(s.label, `step ${s.n} needs a label`).toBeTruthy()
      expect(s.caption, `step ${s.n} needs a caption`).toBeTruthy()
    }
  })

  it('leaves every node reachable by at least one step', () => {
    const touched = new Set(flow.steps.flatMap((s) => [s.from, s.to]))
    for (const n of flow.nodes) {
      expect(touched.has(n.id), `node "${n.id}" is never used by any step`).toBe(true)
    }
  })
})

describe('proactive-alerting flow content (AC 2.3)', () => {
  const flow = flowById('proactive-alerting')

  it('has 8 steps — diagram 4 chains ingest→eb→sfn, which we split so each hop gets a caption', () => {
    expect(flow.steps).toHaveLength(8)
  })

  it('marks BOTH store hops as outbound: the store speaks first, and the reply comes back down', () => {
    const outbound = flow.steps.filter((s) => s.kind === 'outbound').map((s) => s.n)
    expect(outbound).toEqual([1, 7])
    expect(flow.steps.find((s) => s.n === 1).from).toBe('engine')
    expect(flow.steps.find((s) => s.n === 7).to).toBe('display')
  })

  it('ends with a dashed ack that resumes the paused workflow (AC 2.3)', () => {
    const ack = flow.steps.find((s) => s.kind === 'ack')
    expect(ack.n).toBe(8)
    expect(ack.from).toBe('display')
    expect(ack.to).toBe('policy')
    expect(ack.caption).toMatch(/SendTaskSuccess/)
  })

  it('explains the conditional-write dedup gate rather than just saying "dedup"', () => {
    expect(flow.steps.find((s) => s.n === 3).caption).toMatch(/conditional PutItem/i)
  })
})

describe('voice-edge flow content (AC 2.4)', () => {
  const flow = flowById('voice-edge')

  it('has 10 steps', () => {
    expect(flow.steps).toHaveLength(10)
  })

  it('does NOT include the AgentCore harness', () => {
    // ADR-005 narrows ADR-003 to text/analytics: Nova Sonic owns its own tool-use
    // on the voice path. A harness node here would be plain wrong, and it is an
    // easy mistake to make by pattern-matching from the tool-round-trip flow.
    const blob = JSON.stringify(flow).toLowerCase()
    expect(blob).not.toMatch(/agentcore/)
    expect(blob).not.toMatch(/managed harness/)
    expect(flow.nodes.map((n) => n.id)).not.toContain('harness')
  })

  it('keeps AWS credentials out of the browser (the load-bearing security property)', () => {
    expect(flow.steps.find((s) => s.n === 1).caption).toMatch(/no aws credentials/i)
    expect(flow.nodes.find((n) => n.id === 'broker').sublabel).toMatch(/server-side/i)
  })

  it('reuses the same in-process round-trip as the text path', () => {
    expect(flow.steps.find((s) => s.n === 4).label).toMatch(/in-process round-trip/)
    const outbound = flow.steps.filter((s) => s.kind === 'outbound')
    expect(outbound).toHaveLength(1)
    expect(outbound[0].to).toBe('gateway')
  })

  it('records why the voice tool set is curated', () => {
    const captions = flow.steps.map((s) => s.caption).join(' ')
    expect(captions).toMatch(/curated 7-tool set/i)
  })

  it('places the broker, gateway and MCP tool on-premises, with only Sonic and IoT in the cloud', () => {
    const groupOf = (id) => flow.nodes.find((n) => n.id === id).group
    expect(groupOf('browser')).toBe('store')
    expect(groupOf('broker')).toBe('store')
    expect(groupOf('gateway')).toBe('store')
    expect(groupOf('tool')).toBe('store')
    expect(groupOf('sonic')).toBe('cloud')
    expect(groupOf('iot')).toBe('cloud')
  })
})

describe('tool-round-trip flow content', () => {
  const flow = flowById('tool-round-trip')

  it('has the 10 steps from diagram3 (AC 2.2)', () => {
    expect(flow.steps).toHaveLength(10)
  })

  it("marks the store's inbound delivery hop as the emphasized outbound link (AC 3.3)", () => {
    const step5 = flow.steps.find((s) => s.n === 5)
    expect(step5.from).toBe('iot')
    expect(step5.to).toBe('gateway')
    expect(step5.kind).toBe('outbound')
  })

  it('uses the response treatment for the return path (AC 3.4)', () => {
    const kinds = Object.fromEntries(flow.steps.map((s) => [s.n, s.kind]))
    expect(kinds[3]).toBe('response')
    expect(kinds[7]).toBe('response')
    expect(kinds[8]).toBe('response')
    expect(kinds[10]).toBe('response')
  })

  it('marks the re-invoke step bidirectional', () => {
    expect(flow.steps.find((s) => s.n === 9).bidirectional).toBe(true)
  })
})
