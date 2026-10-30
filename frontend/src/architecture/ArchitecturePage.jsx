// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The /architecture route (spec 030).
 *
 * Composes the flow selector, the diagram canvas, the presenter controls and the
 * caption panel. Switching flows resets that flow's timeline to step 1 (AC 2.5) --
 * handled inside `useTimeline`, which restarts whenever the timeline identity
 * changes.
 */
import { useState } from 'react'

import FlowCanvas from './FlowCanvas.jsx'
import { StepCaption } from './StepCaption.jsx'
import { TimelineControls } from './TimelineControls.jsx'
import { FLOWS, flowById, DEFAULT_FLOW_ID } from './flows.js'
import { useTimeline } from './useTimeline.js'

function FlowSelector({ flows, selectedId, onSelect }) {
  return (
    <div className="flex flex-wrap gap-1" role="group" aria-label="Architecture flow">
      {flows.map((flow) => {
        const selected = flow.id === selectedId
        return (
          <button
            key={flow.id}
            type="button"
            onClick={() => onSelect(flow.id)}
            aria-pressed={selected}
            className={
              'rounded-t px-4 py-2 text-sm font-medium transition-colors focus:outline-none ' +
              'focus-visible:ring-2 focus-visible:ring-blue-400 ' +
              (selected
                ? 'bg-gray-700 text-white'
                : 'bg-gray-800 text-gray-400 hover:text-white')
            }
          >
            {flow.label}
          </button>
        )
      })}
    </div>
  )
}

/**
 * Initial flow from the `?flow=<id>` query parameter, falling back to the default
 * when absent or unknown.
 *
 * Deep-linking makes a specific flow shareable in a demo, and makes the Task 10
 * screenshots reproducible (a headless browser can address a flow directly rather
 * than having to click). Read straight from `window.location` rather than via
 * react-router's `useSearchParams`, so the page can be rendered in tests without
 * being wrapped in a Router.
 */
function initialFlowId() {
  if (typeof window === 'undefined') return DEFAULT_FLOW_ID
  const requested = new URLSearchParams(window.location.search).get('flow')
  return requested && flowById(requested) ? requested : DEFAULT_FLOW_ID
}

export function ArchitecturePage() {
  const [flowId, setFlowId] = useState(initialFlowId)
  const flow = flowById(flowId) ?? flowById(DEFAULT_FLOW_ID)

  /** Select a flow and keep the URL in step, so the current view stays shareable. */
  const selectFlow = (id) => {
    setFlowId(id)
    if (typeof window !== 'undefined' && window.history?.replaceState) {
      const url = new URL(window.location.href)
      url.searchParams.set('flow', id)
      window.history.replaceState(null, '', url)
    }
  }

  const {
    state,
    timeline,
    activeStep,
    stepProgress,
    reducedMotion,
    toggle,
    next,
    prev,
    seek,
    setSpeed,
    restart,
  } = useTimeline(flow)

  return (
    <div className="space-y-4">
      <header className="space-y-1">
        {/* semgrep: English-only demo UI; strings are intentionally not internationalized. */}
        {/* nosemgrep: jsx-not-internationalized */}
        <h1 className="text-xl font-bold text-white">Architecture</h1>
        <p className="text-sm text-gray-400">{flow.subtitle}</p>
      </header>

      <FlowSelector flows={FLOWS} selectedId={flow.id} onSelect={selectFlow} />

      {/* Light figure panel inside the dashboard's dark chrome, matching the
          published figures rather than recoloring them. */}
      <div className="rounded-lg bg-white p-4">
        <FlowCanvas flow={flow} activeStepN={activeStep?.n ?? null} progress={stepProgress} />
      </div>

      <TimelineControls
        playing={state.playing}
        elapsedMs={state.elapsedMs}
        speed={state.speed}
        totalMs={timeline.totalMs}
        stepNumber={activeStep?.n ?? 0}
        stepCount={flow.steps.length}
        onToggle={toggle}
        onNext={next}
        onPrev={prev}
        onSeek={seek}
        onSetSpeed={setSpeed}
        onRestart={restart}
      />

      <StepCaption step={activeStep} stepCount={flow.steps.length} />

      {/* nosemgrep: jsx-not-internationalized */}
      {reducedMotion && (
        <p className="text-xs text-amber-300">
          Reduced motion is enabled, so playback does not start automatically. Use Play, or step
          through with Prev / Next.
        </p>
      )}

      {/* nosemgrep: jsx-not-internationalized */}
      <p className="text-xs text-gray-500">
        Transcribed from <code>{flow.docRef}</code>
      </p>
    </div>
  )
}
