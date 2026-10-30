// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useState } from 'react'

export function SimControls({ state, sendControl, connected }) {
  const sim = state?.simulation
  const clock = sim?.clock
  const isPaused = clock?.is_paused ?? true
  const speed = clock?.speed_multiplier ?? 10
  const [localSpeed, setLocalSpeed] = useState(speed)

  const time = clock?.current_time ? new Date(clock.current_time).toLocaleTimeString() : '--:--:--'

  return (
    <div className="flex items-center gap-4 p-3 bg-gray-800 rounded-lg mb-4">
      <div className={`w-2 h-2 rounded-full ${connected ? 'bg-green-400' : 'bg-red-400'}`} />
      <span className="text-sm text-gray-400">{connected ? 'Connected' : 'Disconnected'}</span>

      <div className="text-lg font-mono text-white">{time}</div>

      <button
        onClick={() => sendControl(isPaused ? 'resume' : 'pause')}
        className={`px-3 py-1 rounded text-sm font-medium ${isPaused ? 'bg-green-600 hover:bg-green-500' : 'bg-yellow-600 hover:bg-yellow-500'} text-white`}
      >
        {isPaused ? '▶ Resume' : '⏸ Pause'}
      </button>

      <div className="flex items-center gap-2">
        {/* semgrep: English-only demo UI; strings are intentionally not internationalized. */}
        {/* nosemgrep: jsx-not-internationalized */}
        <span className="text-xs text-gray-400">Speed:</span>
        <input
          type="range" min="1" max="60" value={localSpeed}
          onChange={e => setLocalSpeed(+e.target.value)}
          onMouseUp={() => sendControl('resume', localSpeed)}
          className="w-24"
        />
        <span className="text-sm text-white w-8">{localSpeed}x</span>
      </div>
    </div>
  )
}
