// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Playback model for the architecture visualization -- pure, no React, no DOM.
 *
 * The central idea (spec 030 design § Key Design Decisions #2): **all animation
 * state derives from a single scalar, `elapsedMs`.** Play, pause, scrub, step
 * forward and step back are then the same operation expressed differently, and
 * the active step + its fractional progress are *derived* rather than stored --
 * which makes scrubbing correct by construction and removes the class of bug
 * where a stored step index and the animation disagree.
 */

/**
 * Derive cumulative time offsets for a flow's steps.
 *
 * @param {{n:number,durationMs:number}[]} steps
 * @returns {{entries:{n:number,startMs:number,endMs:number,step:object}[], totalMs:number}}
 */
export function buildTimeline(steps) {
  let acc = 0
  const entries = (steps || []).map((step) => {
    const startMs = acc
    acc += step.durationMs
    return { n: step.n, startMs, endMs: acc, step }
  })
  return { entries, totalMs: acc }
}

/**
 * Initial playback state. `playing` defaults to true, but the caller passes
 * `playing: false` under `prefers-reduced-motion` (AC 5.2).
 */
export function initialState({ playing = true, speed = 1, loop = true } = {}) {
  return { elapsedMs: 0, playing, speed, loop }
}

/** Clamp `ms` into the timeline. */
function clampElapsed(timeline, ms) {
  if (!Number.isFinite(ms)) return 0
  return Math.min(timeline.totalMs, Math.max(0, ms))
}

/**
 * Index of the entry whose `[startMs, endMs)` window contains `elapsedMs`.
 * At exactly `totalMs` (the very end) this reports the LAST entry, so the final
 * frame shows the final hop rather than falling off the end.
 */
export function activeIndex(timeline, state) {
  const { entries } = timeline
  if (entries.length === 0) return -1
  const ms = clampElapsed(timeline, state.elapsedMs)
  const i = entries.findIndex((e) => ms >= e.startMs && ms < e.endMs)
  return i === -1 ? entries.length - 1 : i
}

/** The active timeline entry, or `null` when the flow has no steps. */
export function activeEntry(timeline, state) {
  const i = activeIndex(timeline, state)
  return i === -1 ? null : timeline.entries[i]
}

/** The active step object, or `null`. */
export function activeStep(timeline, state) {
  return activeEntry(timeline, state)?.step ?? null
}

/**
 * Fractional progress (0..1) through the active step -- what the packet position
 * and the connector draw-on are driven from.
 */
export function stepProgress(timeline, state) {
  const entry = activeEntry(timeline, state)
  if (!entry) return 0
  const span = entry.endMs - entry.startMs
  if (span <= 0) return 1
  const ms = clampElapsed(timeline, state.elapsedMs)
  return Math.min(1, Math.max(0, (ms - entry.startMs) / span))
}

/** Overall progress (0..1) across the whole flow -- drives the scrub bar. */
export function totalProgress(timeline, state) {
  if (timeline.totalMs <= 0) return 0
  return clampElapsed(timeline, state.elapsedMs) / timeline.totalMs
}

/**
 * Build the playback reducer for a timeline.
 *
 * Curried over `timeline` so the returned function has the plain
 * `(state, action)` shape `useReducer` expects, while still being able to clamp
 * and snap against the flow's step boundaries.
 *
 * Actions:
 *   TICK      `{deltaMs}`  advance by `deltaMs * speed`; wrap when `loop`,
 *                          otherwise stop at the end
 *   PLAY                   resume; restarts first if parked at the end
 *   PAUSE                  suspend
 *   NEXT / PREV            snap to the adjacent step's `startMs`, clamped at
 *                          both ends (AC 4.2)
 *   SEEK      `{ms}`       clamp into `[0, totalMs]` (AC 4.4)
 *   SET_SPEED `{speed}`    change rate without disturbing position/playing (AC 4.3)
 *   RESTART                back to the first step, leaving `playing` alone
 */
export function createReducer(timeline) {
  const { entries, totalMs } = timeline

  return function reducer(state, action) {
    switch (action.type) {
      case 'TICK': {
        if (!state.playing || totalMs <= 0) return state
        const delta = Number.isFinite(action.deltaMs) ? action.deltaMs : 0
        const next = state.elapsedMs + delta * state.speed
        if (next < totalMs) return { ...state, elapsedMs: Math.max(0, next) }
        // Past the end: wrap when looping (AC 4.6), else park at the end and stop.
        if (state.loop) {
          // `%` keeps the leftover so a long frame doesn't lose time.
          return { ...state, elapsedMs: next % totalMs }
        }
        return { ...state, elapsedMs: totalMs, playing: false }
      }

      case 'PLAY': {
        // Parked at the end with looping off: replay from the top rather than
        // "playing" while visibly stuck.
        if (!state.loop && totalMs > 0 && state.elapsedMs >= totalMs) {
          return { ...state, elapsedMs: 0, playing: true }
        }
        return { ...state, playing: true }
      }

      case 'PAUSE':
        return { ...state, playing: false }

      case 'NEXT': {
        const i = activeIndex(timeline, state)
        if (i === -1) return state
        const target = entries[Math.min(entries.length - 1, i + 1)]
        return { ...state, elapsedMs: target.startMs }
      }

      case 'PREV': {
        const i = activeIndex(timeline, state)
        if (i === -1) return state
        const target = entries[Math.max(0, i - 1)]
        return { ...state, elapsedMs: target.startMs }
      }

      case 'SEEK':
        return { ...state, elapsedMs: clampElapsed(timeline, action.ms) }

      case 'SET_SPEED': {
        const speed = Number.isFinite(action.speed) && action.speed > 0 ? action.speed : state.speed
        return { ...state, speed }
      }

      case 'RESTART':
        return { ...state, elapsedMs: 0 }

      default:
        return state
    }
  }
}
