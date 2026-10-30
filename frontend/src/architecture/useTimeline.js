// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * requestAnimationFrame driver for the architecture animation (spec 030).
 *
 * The hook owns only the *clock*: it feeds real frame deltas into the pure
 * reducer in `timeline.js` and exposes derived values. All playback semantics
 * live in the reducer, which is why they are testable without a browser.
 *
 * Resource behavior (AC 5.1, 5.2, 5.5) -- this can run on an always-on in-store
 * display, so:
 *   - the loop is suspended while `document.hidden`, without disturbing the
 *     user's own play/pause intent;
 *   - `prefers-reduced-motion: reduce` means no autoplay and a static render,
 *     while stepping and scrubbing still work;
 *   - the pending frame is cancelled and listeners removed on unmount.
 */
import { useCallback, useEffect, useMemo, useReducer, useState } from 'react'

import {
  activeIndex as selectActiveIndex,
  activeStep as selectActiveStep,
  buildTimeline,
  createReducer,
  initialState,
  stepProgress as selectStepProgress,
  totalProgress as selectTotalProgress,
} from './timeline.js'

/** True when the user has asked the OS to reduce motion. Safe under jsdom/SSR. */
export function prefersReducedMotion() {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

/** Current document visibility. Safe under jsdom/SSR. */
function documentVisible() {
  if (typeof document === 'undefined') return true
  return !document.hidden
}

export function useTimeline(flow, { loop = true } = {}) {
  const timeline = useMemo(() => buildTimeline(flow.steps), [flow.steps])
  const reducer = useMemo(() => createReducer(timeline), [timeline])

  // Read once at mount: reduced-motion means "do not autoplay" (AC 5.2).
  const reducedMotion = useMemo(() => prefersReducedMotion(), [])

  const [state, dispatch] = useReducer(reducer, undefined, () =>
    initialState({ playing: !reducedMotion, loop }),
  )

  const [visible, setVisible] = useState(documentVisible)

  // Switching flows resets the timeline to step 1 (AC 2.5). Also keeps elapsedMs
  // from pointing past the end of a shorter flow.
  useEffect(() => {
    dispatch({ type: 'RESTART' })
  }, [timeline])

  // Suspend while the tab is hidden, and resume when it comes back (AC 5.1).
  // Tracked separately from `state.playing` so a manual pause is not undone by
  // the tab regaining focus.
  useEffect(() => {
    if (typeof document === 'undefined') return undefined
    const onChange = () => setVisible(documentVisible())
    document.addEventListener('visibilitychange', onChange)
    return () => document.removeEventListener('visibilitychange', onChange)
  }, [])

  // The frame loop. Restarted whenever it should be running or not; the pending
  // frame is always cancelled on cleanup (AC 5.5).
  useEffect(() => {
    if (!state.playing || !visible) return undefined
    if (typeof window === 'undefined' || typeof window.requestAnimationFrame !== 'function') {
      return undefined
    }

    let handle = 0
    let previous = null
    const frame = (now) => {
      if (previous !== null) dispatch({ type: 'TICK', deltaMs: now - previous })
      previous = now
      handle = window.requestAnimationFrame(frame)
    }
    handle = window.requestAnimationFrame(frame)
    return () => window.cancelAnimationFrame(handle)
  }, [state.playing, visible])

  // --- action helpers (stable identities for the controls) ------------------
  const play = useCallback(() => dispatch({ type: 'PLAY' }), [])
  const pause = useCallback(() => dispatch({ type: 'PAUSE' }), [])
  const next = useCallback(() => dispatch({ type: 'NEXT' }), [])
  const prev = useCallback(() => dispatch({ type: 'PREV' }), [])
  const restart = useCallback(() => dispatch({ type: 'RESTART' }), [])
  const seek = useCallback((ms) => dispatch({ type: 'SEEK', ms }), [])
  const setSpeed = useCallback((speed) => dispatch({ type: 'SET_SPEED', speed }), [])
  const toggle = useCallback(
    () => dispatch({ type: state.playing ? 'PAUSE' : 'PLAY' }),
    [state.playing],
  )

  return {
    timeline,
    state,
    reducedMotion,
    // Derived view of the current frame -- what the canvas renders from.
    activeIndex: selectActiveIndex(timeline, state),
    activeStep: selectActiveStep(timeline, state),
    stepProgress: selectStepProgress(timeline, state),
    totalProgress: selectTotalProgress(timeline, state),
    // Actions
    play,
    pause,
    toggle,
    next,
    prev,
    seek,
    setSpeed,
    restart,
  }
}
