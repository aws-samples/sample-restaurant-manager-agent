// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Caption panel for the active step (spec 030).
 *
 * `aria-live="polite"` so a step change is announced to a screen reader without
 * stealing focus (AC 5.4) -- the presenter is driving the controls, so focus must
 * stay where they put it.
 *
 * The caption is where the non-obvious design points live (the outbound-only
 * connection, the dedup cache, why the re-invoke sends two messages), which is
 * the whole reason this page exists instead of a still image.
 */
import { STEP_STYLES } from './theme.js'

/** Human-readable meaning of each connector treatment. */
const KIND_LABEL = {
  request: 'forward path',
  response: 'return path',
  outbound: 'store-initiated outbound mTLS',
  ack: 'out-of-band acknowledgment',
}

export function StepCaption({ step, stepCount }) {
  const style = step ? (STEP_STYLES[step.kind] ?? STEP_STYLES.request) : null

  return (
    <div
      aria-live="polite"
      aria-atomic="true"
      className="min-h-[6.5rem] rounded-lg bg-gray-800 p-4"
      data-testid="step-caption"
    >
      {step ? (
        <>
          <div className="mb-1 flex items-center gap-2">
            <span
              aria-hidden="true"
              className="inline-block h-3 w-3 rounded-full"
              style={{ backgroundColor: style.stroke }}
            />
            {/* semgrep: English-only demo UI; strings are intentionally not internationalized. */}
            {/* nosemgrep: jsx-not-internationalized */}
            <span className="text-sm font-semibold text-white">
              Step {step.n} of {stepCount}: {step.label}
            </span>
            <span className="text-xs text-gray-400">({KIND_LABEL[step.kind] ?? step.kind})</span>
          </div>
          <p className="text-sm leading-relaxed text-gray-300">{step.caption}</p>
        </>
      ) : ( // nosemgrep: jsx-not-internationalized
        <p className="text-sm text-gray-400">No step selected.</p>
      )}
    </div>
  )
}
