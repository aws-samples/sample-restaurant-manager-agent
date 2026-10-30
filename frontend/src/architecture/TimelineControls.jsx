// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Presenter controls for the architecture animation (spec 030).
 *
 * Every control is a real `<button>` / `<input>` with an explicit `aria-label`
 * and a visible focus ring (AC 4.7), so the whole panel is keyboard-operable --
 * which matters because this gets driven live in front of an audience.
 */

/** Selectable playback rates (AC 4.3). */
const SPEEDS = [0.5, 1, 2]

const BUTTON_CLASSES =
  'rounded bg-gray-700 px-3 py-1.5 text-sm font-medium text-white transition-colors ' +
  'hover:bg-gray-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 ' +
  'disabled:cursor-not-allowed disabled:opacity-40'

export function TimelineControls({
  playing,
  elapsedMs,
  speed,
  totalMs,
  stepNumber,
  stepCount,
  onToggle,
  onNext,
  onPrev,
  onSeek,
  onSetSpeed,
  onRestart,
}) {
  const atFirst = stepNumber <= 1
  const atLast = stepNumber >= stepCount

  return (
    <div className="flex flex-wrap items-center gap-3 rounded-lg bg-gray-800 p-3">
      <button
        type="button"
        onClick={onToggle}
        className={BUTTON_CLASSES}
        aria-label={playing ? 'Pause animation' : 'Play animation'}
      >
        {playing ? '⏸ Pause' : '▶ Play'}
      </button>

      <div className="flex gap-1">
        <button
          type="button"
          onClick={onPrev}
          disabled={atFirst}
          className={BUTTON_CLASSES}
          aria-label="Previous step"
        >
          ⏮ Prev
        </button>
        {/* semgrep: English-only demo UI; strings are intentionally not internationalized. */}
        {/* nosemgrep: jsx-not-internationalized */}
        <button
          type="button"
          onClick={onNext}
          disabled={atLast}
          className={BUTTON_CLASSES}
          aria-label="Next step"
        >
          Next ⏭
        </button>
      </div>

      <button type="button" onClick={onRestart} className={BUTTON_CLASSES} aria-label="Restart flow">
        ↺ Restart
      </button>

      {/* Speed. `aria-pressed` communicates which rate is selected. */}
      <div className="flex items-center gap-1" role="group" aria-label="Playback speed">
        {SPEEDS.map((rate) => (
          <button
            key={rate}
            type="button"
            onClick={() => onSetSpeed(rate)}
            aria-label={`${rate}× speed`}
            aria-pressed={speed === rate}
            className={
              'rounded px-2.5 py-1.5 text-sm font-medium transition-colors focus:outline-none ' +
              'focus-visible:ring-2 focus-visible:ring-blue-400 ' +
              (speed === rate
                ? 'bg-blue-600 text-white'
                : 'bg-gray-700 text-gray-300 hover:bg-gray-600')
            }
          >
            {rate}×
          </button>
        ))}
      </div>

      {/* Scrub. Bound directly to elapsedMs, so seeking and playback share one
          source of truth (AC 4.4). */}
      <label className="flex min-w-[12rem] flex-1 items-center gap-2 text-xs text-gray-400">
        {/* nosemgrep: jsx-not-internationalized */}
        <span className="sr-only">Timeline position</span>
        <input
          type="range"
          min={0}
          max={totalMs}
          step={10}
          value={elapsedMs}
          onChange={(e) => onSeek(Number(e.target.value))}
          aria-label="Timeline position"
          aria-valuetext={`Step ${stepNumber} of ${stepCount}`}
          className="w-full accent-blue-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-400"
        />
        <span className="tabular-nums whitespace-nowrap">
          {(elapsedMs / 1000).toFixed(1)}s / {(totalMs / 1000).toFixed(1)}s
        </span>
      </label>
    </div>
  )
}
