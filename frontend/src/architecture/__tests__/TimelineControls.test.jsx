// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Presenter controls + caption tests (spec 030 Task 8).
 *
 * These drive the real page (not the controls in isolation) because the behaviors
 * that matter -- play actually advancing, Next moving the caption, scrubbing
 * selecting a step -- only exist once the controls are wired to the timeline hook.
 *
 * `prefers-reduced-motion` is forced ON for most tests so the page starts paused
 * and assertions are deterministic; autoplay is exercised separately.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'

import { ArchitecturePage } from '../ArchitecturePage.jsx'
import { flowById, DEFAULT_FLOW_ID } from '../flows.js'
import { buildTimeline } from '../timeline.js'

const flow = flowById(DEFAULT_FLOW_ID)
const timeline = buildTimeline(flow.steps)

function installRaf() {
  const queue = new Map()
  let nextId = 1
  vi.stubGlobal(
    'requestAnimationFrame',
    vi.fn((cb) => {
      const id = nextId
      nextId += 1
      queue.set(id, cb)
      return id
    }),
  )
  vi.stubGlobal(
    'cancelAnimationFrame',
    vi.fn((id) => queue.delete(id)),
  )
  return {
    pending: () => queue.size,
    frame(t) {
      const cbs = [...queue.values()]
      queue.clear()
      for (const cb of cbs) cb(t)
    },
  }
}

function setReducedMotion(matches) {
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => ({ matches, media: '', addEventListener() {}, removeEventListener() {} })),
  )
}

let raf

beforeEach(() => {
  raf = installRaf()
  setReducedMotion(true) // start paused unless a test opts into autoplay
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

/** The caption's current step number, read from the live region. */
function captionStepNumber() {
  const text = screen.getByTestId('step-caption').textContent || ''
  const m = text.match(/Step (\d+) of/)
  return m ? Number(m[1]) : null
}

describe('TimelineControls — presence and accessibility (AC 4.7)', () => {
  beforeEach(() => render(<ArchitecturePage />))

  it('exposes play, step, restart and speed controls as labelled buttons', () => {
    expect(screen.getByRole('button', { name: /play animation/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /previous step/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /next step/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /restart flow/i })).toBeInTheDocument()
    for (const rate of ['0.5', '1', '2']) {
      expect(screen.getByRole('button', { name: `${rate}× speed` })).toBeInTheDocument()
    }
  })

  it('exposes the scrub as a labelled slider bound to the timeline length (AC 4.4)', () => {
    const slider = screen.getByRole('slider', { name: /timeline position/i })
    expect(slider).toHaveAttribute('max', String(timeline.totalMs))
    expect(slider).toHaveValue(String(0))
  })
})

describe('play / pause (AC 4.1)', () => {
  it('starts playback on click and advances the clock', () => {
    render(<ArchitecturePage />)
    // Reduced motion => paused, so no frame is queued yet.
    expect(raf.pending()).toBe(0)

    fireEvent.click(screen.getByRole('button', { name: /play animation/i }))

    // The button flips to Pause, and the loop is now running.
    expect(screen.getByRole('button', { name: /pause animation/i })).toBeInTheDocument()
    expect(raf.pending()).toBe(1)

    // Two frames: the first primes the timestamp, the second delivers a delta
    // large enough to cross into step 2.
    act(() => raf.frame(0))
    act(() => raf.frame(timeline.entries[1].startMs + 10))
    expect(captionStepNumber()).toBe(2)
  })

  it('pauses again on a second click and stops queueing frames', () => {
    render(<ArchitecturePage />)
    fireEvent.click(screen.getByRole('button', { name: /play animation/i }))
    fireEvent.click(screen.getByRole('button', { name: /pause animation/i }))
    expect(screen.getByRole('button', { name: /play animation/i })).toBeInTheDocument()
    expect(raf.pending()).toBe(0)
  })

  it('autoplays when reduced motion is not requested', () => {
    setReducedMotion(false)
    render(<ArchitecturePage />)
    expect(screen.getByRole('button', { name: /pause animation/i })).toBeInTheDocument()
    expect(raf.pending()).toBe(1)
  })
})

describe('stepping (AC 4.2)', () => {
  beforeEach(() => render(<ArchitecturePage />))

  it('advances the caption to step n+1 on Next', () => {
    expect(captionStepNumber()).toBe(1)
    fireEvent.click(screen.getByRole('button', { name: /next step/i }))
    expect(captionStepNumber()).toBe(2)
    fireEvent.click(screen.getByRole('button', { name: /next step/i }))
    expect(captionStepNumber()).toBe(3)
  })

  it('goes back on Prev', () => {
    fireEvent.click(screen.getByRole('button', { name: /next step/i }))
    fireEvent.click(screen.getByRole('button', { name: /next step/i }))
    expect(captionStepNumber()).toBe(3)
    fireEvent.click(screen.getByRole('button', { name: /previous step/i }))
    expect(captionStepNumber()).toBe(2)
  })

  it('clamps at the first step (Prev disabled)', () => {
    expect(captionStepNumber()).toBe(1)
    expect(screen.getByRole('button', { name: /previous step/i })).toBeDisabled()
  })

  it('clamps at the last step (Next disabled once there)', () => {
    const last = flow.steps.length
    for (let i = 1; i < last; i += 1) {
      fireEvent.click(screen.getByRole('button', { name: /next step/i }))
    }
    expect(captionStepNumber()).toBe(last)
    expect(screen.getByRole('button', { name: /next step/i })).toBeDisabled()
  })

  it('restarts to step 1', () => {
    fireEvent.click(screen.getByRole('button', { name: /next step/i }))
    fireEvent.click(screen.getByRole('button', { name: /next step/i }))
    fireEvent.click(screen.getByRole('button', { name: /restart flow/i }))
    expect(captionStepNumber()).toBe(1)
  })
})

describe('scrubbing (AC 4.4)', () => {
  it('selects the step containing the sought timestamp', () => {
    render(<ArchitecturePage />)
    const slider = screen.getByRole('slider', { name: /timeline position/i })

    // Aim at the middle of step 5 rather than hardcoding a number, so the test
    // survives duration edits.
    const target = flow.steps.length >= 5 ? timeline.entries[4] : timeline.entries[0]
    const midpoint = Math.round((target.startMs + target.endMs) / 2)

    fireEvent.change(slider, { target: { value: String(midpoint) } })

    expect(captionStepNumber()).toBe(target.n)
    expect(slider).toHaveValue(String(midpoint))
  })

  it('clamps a seek beyond the end to the last step', () => {
    render(<ArchitecturePage />)
    const slider = screen.getByRole('slider', { name: /timeline position/i })
    fireEvent.change(slider, { target: { value: String(timeline.totalMs) } })
    expect(captionStepNumber()).toBe(flow.steps.length)
  })
})

describe('speed (AC 4.3)', () => {
  it('marks the selected rate pressed and leaves position alone', () => {
    render(<ArchitecturePage />)
    fireEvent.click(screen.getByRole('button', { name: /next step/i }))
    const before = captionStepNumber()

    const fast = screen.getByRole('button', { name: '2× speed' })
    fireEvent.click(fast)

    expect(fast).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: '1× speed' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
    expect(captionStepNumber()).toBe(before) // AC 4.3: no restart
  })

  it('defaults to 1×', () => {
    render(<ArchitecturePage />)
    expect(screen.getByRole('button', { name: '1× speed' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })
})

describe('StepCaption (AC 4.5, 5.4)', () => {
  beforeEach(() => render(<ArchitecturePage />))

  it('is a polite live region so step changes are announced', () => {
    const caption = screen.getByTestId('step-caption')
    expect(caption).toHaveAttribute('aria-live', 'polite')
  })

  it("shows the active step's number, edge label and explanatory prose", () => {
    const step1 = flow.steps[0]
    const caption = screen.getByTestId('step-caption')
    expect(caption.textContent).toContain(`Step 1 of ${flow.steps.length}`)
    expect(caption.textContent).toContain(step1.label)
    expect(caption.textContent).toContain(step1.caption)
  })

  it('updates the prose when the step changes', () => {
    fireEvent.click(screen.getByRole('button', { name: /next step/i }))
    expect(screen.getByTestId('step-caption').textContent).toContain(flow.steps[1].caption)
  })

  it('names the connector treatment, so the outbound hop is explained', () => {
    const outbound = flow.steps.find((s) => s.kind === 'outbound')
    for (let i = 1; i < outbound.n; i += 1) {
      fireEvent.click(screen.getByRole('button', { name: /next step/i }))
    }
    expect(captionStepNumber()).toBe(outbound.n)
    expect(screen.getByTestId('step-caption').textContent).toMatch(/outbound mTLS/i)
  })
})
