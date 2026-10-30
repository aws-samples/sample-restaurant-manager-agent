// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Timeline / playback-model tests (spec 030 Task 5).
 *
 * This is the whole playback model verified without a browser: TICK scaling by
 * speed, NEXT/PREV snapping and clamping, wrap-at-end when looping, and SEEK
 * landing on the right step with the right fractional progress.
 */
import { describe, it, expect } from 'vitest'

import {
  buildTimeline,
  createReducer,
  initialState,
  activeIndex,
  activeStep,
  activeEntry,
  stepProgress,
  totalProgress,
} from '../timeline.js'
import { flowById } from '../flows.js'

// Three steps of 1000ms each -> totalMs 3000, boundaries at 0 / 1000 / 2000.
const STEPS = [
  { n: 1, durationMs: 1000, label: 'one' },
  { n: 2, durationMs: 1000, label: 'two' },
  { n: 3, durationMs: 1000, label: 'three' },
]

const timeline = buildTimeline(STEPS)
const reduce = createReducer(timeline)
const at = (ms, extra = {}) => ({ ...initialState(), elapsedMs: ms, ...extra })

describe('buildTimeline', () => {
  it('derives cumulative offsets and a total', () => {
    expect(timeline.totalMs).toBe(3000)
    expect(timeline.entries.map((e) => [e.startMs, e.endMs])).toEqual([
      [0, 1000],
      [1000, 2000],
      [2000, 3000],
    ])
  })

  it('keeps each step reachable from its entry', () => {
    expect(timeline.entries[1].step.label).toBe('two')
    expect(timeline.entries[1].n).toBe(2)
  })

  it('handles an empty step list', () => {
    const empty = buildTimeline([])
    expect(empty.totalMs).toBe(0)
    expect(empty.entries).toEqual([])
  })

  it('builds a real flow timeline', () => {
    const flow = flowById('tool-round-trip')
    const t = buildTimeline(flow.steps)
    expect(t.entries).toHaveLength(10)
    expect(t.totalMs).toBe(flow.steps.reduce((s, x) => s + x.durationMs, 0))
    expect(t.entries[0].startMs).toBe(0)
  })
})

describe('selectors', () => {
  it('reports the step whose window contains elapsedMs', () => {
    expect(activeStep(timeline, at(0)).n).toBe(1)
    expect(activeStep(timeline, at(999)).n).toBe(1)
    expect(activeStep(timeline, at(1000)).n).toBe(2) // boundary belongs to the next step
    expect(activeStep(timeline, at(2500)).n).toBe(3)
  })

  it('reports the LAST step at exactly totalMs, not nothing', () => {
    expect(activeStep(timeline, at(3000)).n).toBe(3)
    expect(stepProgress(timeline, at(3000))).toBe(1)
  })

  it('computes fractional progress within the active step', () => {
    expect(stepProgress(timeline, at(0))).toBe(0)
    expect(stepProgress(timeline, at(500))).toBe(0.5)
    expect(stepProgress(timeline, at(1250))).toBe(0.25) // 250ms into step 2
  })

  it('computes overall progress for the scrub bar', () => {
    expect(totalProgress(timeline, at(0))).toBe(0)
    expect(totalProgress(timeline, at(1500))).toBe(0.5)
    expect(totalProgress(timeline, at(3000))).toBe(1)
  })

  it('degrades safely on an empty timeline', () => {
    const empty = buildTimeline([])
    expect(activeIndex(empty, at(0))).toBe(-1)
    expect(activeEntry(empty, at(0))).toBeNull()
    expect(activeStep(empty, at(0))).toBeNull()
    expect(stepProgress(empty, at(0))).toBe(0)
    expect(totalProgress(empty, at(0))).toBe(0)
  })
})

describe('TICK', () => {
  it('advances by deltaMs * speed', () => {
    expect(reduce(at(0), { type: 'TICK', deltaMs: 100 }).elapsedMs).toBe(100)
    expect(reduce(at(0, { speed: 2 }), { type: 'TICK', deltaMs: 100 }).elapsedMs).toBe(200)
    expect(reduce(at(0, { speed: 0.5 }), { type: 'TICK', deltaMs: 100 }).elapsedMs).toBe(50)
  })

  it('does nothing while paused', () => {
    const paused = at(500, { playing: false })
    expect(reduce(paused, { type: 'TICK', deltaMs: 100 })).toBe(paused)
  })

  it('wraps past the end when looping, keeping the leftover', () => {
    // 2900 + 200 = 3100 -> wraps to 100, so a long frame does not lose time.
    expect(reduce(at(2900), { type: 'TICK', deltaMs: 200 }).elapsedMs).toBe(100)
  })

  it('parks at the end and stops when not looping', () => {
    const next = reduce(at(2900, { loop: false }), { type: 'TICK', deltaMs: 500 })
    expect(next.elapsedMs).toBe(3000)
    expect(next.playing).toBe(false)
  })

  it('ignores a non-finite delta', () => {
    expect(reduce(at(500), { type: 'TICK', deltaMs: Number.NaN }).elapsedMs).toBe(500)
  })

  it('is a no-op on an empty timeline', () => {
    const emptyReduce = createReducer(buildTimeline([]))
    const s = at(0)
    expect(emptyReduce(s, { type: 'TICK', deltaMs: 100 })).toBe(s)
  })
})

describe('NEXT / PREV', () => {
  it('snap to the adjacent step boundary from mid-step', () => {
    // Mid step 1 -> start of step 2.
    expect(reduce(at(500), { type: 'NEXT' }).elapsedMs).toBe(1000)
    // Mid step 2 -> start of step 1 (symmetric with NEXT).
    expect(reduce(at(1500), { type: 'PREV' }).elapsedMs).toBe(0)
  })

  it('clamp at the last step', () => {
    expect(reduce(at(2500), { type: 'NEXT' }).elapsedMs).toBe(2000)
    expect(reduce(at(3000), { type: 'NEXT' }).elapsedMs).toBe(2000)
  })

  it('clamp at the first step', () => {
    expect(reduce(at(0), { type: 'PREV' }).elapsedMs).toBe(0)
    expect(reduce(at(500), { type: 'PREV' }).elapsedMs).toBe(0)
  })

  it('leave playing state alone so you can step while paused', () => {
    expect(reduce(at(500, { playing: false }), { type: 'NEXT' }).playing).toBe(false)
    expect(reduce(at(500, { playing: true }), { type: 'NEXT' }).playing).toBe(true)
  })

  it('land exactly on a step boundary, so progress restarts at 0', () => {
    const next = reduce(at(500), { type: 'NEXT' })
    expect(activeStep(timeline, next).n).toBe(2)
    expect(stepProgress(timeline, next)).toBe(0)
  })
})

describe('SEEK', () => {
  it('selects the expected step and fractional progress mid-step', () => {
    const s = reduce(at(0), { type: 'SEEK', ms: 2400 })
    expect(activeStep(timeline, s).n).toBe(3)
    expect(stepProgress(timeline, s)).toBeCloseTo(0.4, 10)
  })

  it('clamps outside the timeline', () => {
    expect(reduce(at(0), { type: 'SEEK', ms: -100 }).elapsedMs).toBe(0)
    expect(reduce(at(0), { type: 'SEEK', ms: 99999 }).elapsedMs).toBe(3000)
  })

  it('treats a non-finite target as 0', () => {
    expect(reduce(at(1500), { type: 'SEEK', ms: Number.NaN }).elapsedMs).toBe(0)
  })
})

describe('PLAY / PAUSE', () => {
  it('toggle playing without moving the clock', () => {
    expect(reduce(at(700, { playing: false }), { type: 'PLAY' })).toMatchObject({
      playing: true,
      elapsedMs: 700,
    })
    expect(reduce(at(700), { type: 'PAUSE' })).toMatchObject({ playing: false, elapsedMs: 700 })
  })

  it('replays from the top when parked at the end with looping off', () => {
    const parked = at(3000, { playing: false, loop: false })
    expect(reduce(parked, { type: 'PLAY' })).toMatchObject({ playing: true, elapsedMs: 0 })
  })
})

describe('SET_SPEED', () => {
  it('changes rate without restarting playback (AC 4.3)', () => {
    const next = reduce(at(1234), { type: 'SET_SPEED', speed: 2 })
    expect(next.speed).toBe(2)
    expect(next.elapsedMs).toBe(1234)
    expect(next.playing).toBe(true)
  })

  it('rejects a non-positive or non-finite speed', () => {
    expect(reduce(at(0), { type: 'SET_SPEED', speed: 0 }).speed).toBe(1)
    expect(reduce(at(0), { type: 'SET_SPEED', speed: -2 }).speed).toBe(1)
    expect(reduce(at(0), { type: 'SET_SPEED', speed: Number.NaN }).speed).toBe(1)
  })
})

describe('RESTART', () => {
  it('returns to the first step, leaving playing alone (used on flow switch)', () => {
    expect(reduce(at(2500), { type: 'RESTART' })).toMatchObject({ elapsedMs: 0, playing: true })
    expect(reduce(at(2500, { playing: false }), { type: 'RESTART' })).toMatchObject({
      elapsedMs: 0,
      playing: false,
    })
  })
})

describe('unknown actions', () => {
  it('return the same state object', () => {
    const s = at(500)
    expect(reduce(s, { type: 'NOT_A_REAL_ACTION' })).toBe(s)
  })
})
