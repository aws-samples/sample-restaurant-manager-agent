// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * useTimeline hook tests (spec 030 Task 7).
 *
 * `requestAnimationFrame` is replaced with a hand-driven queue so frames advance
 * deterministically -- no timers, no waiting. The hook feeds real deltas into the
 * pure reducer, so what is under test here is the *clock plumbing*: does it run
 * when it should, stop when it shouldn't, and clean up after itself.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'

import { useTimeline } from '../useTimeline.js'

/** Two steps of 1000ms -> totalMs 2000. Only `steps` is read by the hook. */
const FLOW = {
  id: 'test-flow',
  steps: [
    { n: 1, durationMs: 1000, label: 'one', kind: 'request' },
    { n: 2, durationMs: 1000, label: 'two', kind: 'response' },
  ],
}

/** Install a hand-driven rAF queue. */
function installRaf() {
  const queue = new Map()
  let nextId = 1
  const raf = vi.fn((cb) => {
    const id = nextId
    nextId += 1
    queue.set(id, cb)
    return id
  })
  const cancel = vi.fn((id) => queue.delete(id))
  vi.stubGlobal('requestAnimationFrame', raf)
  vi.stubGlobal('cancelAnimationFrame', cancel)
  return {
    raf,
    cancel,
    pending: () => queue.size,
    /** Run every queued callback with timestamp `t`. */
    frame(t) {
      const callbacks = [...queue.values()]
      queue.clear()
      for (const cb of callbacks) cb(t)
    },
  }
}

/** Force the reduced-motion media query on or off. */
function setReducedMotion(matches) {
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => ({ matches, media: '', addEventListener() {}, removeEventListener() {} })),
  )
}

/** Force `document.hidden` and fire the corresponding event. */
function setHidden(hidden) {
  Object.defineProperty(document, 'hidden', { value: hidden, configurable: true })
  document.dispatchEvent(new Event('visibilitychange'))
}

let rafHarness

beforeEach(() => {
  rafHarness = installRaf()
  setReducedMotion(false)
  Object.defineProperty(document, 'hidden', { value: false, configurable: true })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

/** Advance the clock by `ms`, accounting for the loop's first priming frame. */
function advance(ms, from = 0) {
  act(() => rafHarness.frame(from)) // primes `previous`; no dispatch yet
  act(() => rafHarness.frame(from + ms))
}

describe('useTimeline', () => {
  it('autoplays and advances the clock while playing', () => {
    const { result } = renderHook(() => useTimeline(FLOW))
    expect(result.current.state.playing).toBe(true)
    expect(result.current.state.elapsedMs).toBe(0)

    advance(250)
    expect(result.current.state.elapsedMs).toBe(250)
  })

  it('derives the active step and its progress from the clock', () => {
    const { result } = renderHook(() => useTimeline(FLOW))
    advance(1500)
    expect(result.current.activeStep.n).toBe(2)
    expect(result.current.stepProgress).toBeCloseTo(0.5, 10)
    expect(result.current.totalProgress).toBeCloseTo(0.75, 10)
  })

  it('stops advancing once paused, and does not queue more frames', () => {
    const { result } = renderHook(() => useTimeline(FLOW))
    advance(300)
    expect(result.current.state.elapsedMs).toBe(300)

    act(() => result.current.pause())
    expect(result.current.state.playing).toBe(false)
    expect(rafHarness.pending()).toBe(0)

    // Even if a stray frame fires, a paused TICK is a no-op.
    act(() => rafHarness.frame(9999))
    expect(result.current.state.elapsedMs).toBe(300)
  })

  it('resumes advancing after play', () => {
    const { result } = renderHook(() => useTimeline(FLOW))
    act(() => result.current.pause())
    act(() => result.current.play())
    expect(result.current.state.playing).toBe(true)
    advance(100)
    expect(result.current.state.elapsedMs).toBe(100)
  })

  it('scales advancement by the selected speed', () => {
    const { result } = renderHook(() => useTimeline(FLOW))
    act(() => result.current.setSpeed(2))
    advance(100)
    expect(result.current.state.elapsedMs).toBe(200)
  })

  it('does not autoplay under prefers-reduced-motion, but still steps and seeks (AC 5.2)', () => {
    setReducedMotion(true)
    const { result } = renderHook(() => useTimeline(FLOW))

    expect(result.current.reducedMotion).toBe(true)
    expect(result.current.state.playing).toBe(false)
    expect(rafHarness.pending()).toBe(0)

    act(() => result.current.next())
    expect(result.current.activeStep.n).toBe(2)

    act(() => result.current.seek(1500))
    expect(result.current.state.elapsedMs).toBe(1500)
  })

  it('suspends the loop while the document is hidden and resumes when visible (AC 5.1)', () => {
    const { result } = renderHook(() => useTimeline(FLOW))
    advance(200)
    expect(result.current.state.elapsedMs).toBe(200)

    act(() => setHidden(true))
    expect(rafHarness.pending()).toBe(0)
    act(() => rafHarness.frame(5000))
    expect(result.current.state.elapsedMs).toBe(200) // frozen while hidden

    act(() => setHidden(false))
    expect(result.current.state.playing).toBe(true)
    advance(100, 6000)
    expect(result.current.state.elapsedMs).toBe(300)
  })

  it('does not resume a manually paused animation when the tab regains focus', () => {
    const { result } = renderHook(() => useTimeline(FLOW))
    act(() => result.current.pause())
    act(() => setHidden(true))
    act(() => setHidden(false))
    expect(result.current.state.playing).toBe(false)
    expect(rafHarness.pending()).toBe(0)
  })

  it('cancels the pending frame on unmount (AC 5.5)', () => {
    const { unmount } = renderHook(() => useTimeline(FLOW))
    expect(rafHarness.pending()).toBe(1)
    unmount()
    expect(rafHarness.cancel).toHaveBeenCalled()
    expect(rafHarness.pending()).toBe(0)
  })

  it('removes its visibilitychange listener on unmount (AC 5.5)', () => {
    const remove = vi.spyOn(document, 'removeEventListener')
    const { unmount } = renderHook(() => useTimeline(FLOW))
    unmount()
    expect(remove).toHaveBeenCalledWith('visibilitychange', expect.any(Function))
  })

  it('resets to the first step when the flow changes (AC 2.5)', () => {
    const other = {
      id: 'other',
      steps: [{ n: 1, durationMs: 500, label: 'only', kind: 'request' }],
    }
    const { result, rerender } = renderHook(({ flow }) => useTimeline(flow), {
      initialProps: { flow: FLOW },
    })
    advance(1500)
    expect(result.current.state.elapsedMs).toBe(1500)

    rerender({ flow: other })
    expect(result.current.state.elapsedMs).toBe(0)
    expect(result.current.activeStep.n).toBe(1)
  })

  it('loops back to the start past the end of the flow (AC 4.6)', () => {
    const { result } = renderHook(() => useTimeline(FLOW))
    act(() => result.current.seek(1900))
    advance(200, 1000)
    // 1900 + 200 = 2100 -> wraps past totalMs 2000 to 100.
    expect(result.current.state.elapsedMs).toBe(100)
  })
})
