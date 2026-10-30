// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

const STATUS_STYLE = {
  normal: 'bg-green-600', warning: 'bg-yellow-600', alert: 'bg-red-600',
}

export function EquipmentPanel({ equipment }) {
  if (!equipment) return null
  const items = equipment.equipment || []

  return (
    <div className="grid grid-cols-2 gap-3">
      {items.map(e => (
        <div key={e.id} className="bg-gray-700 rounded p-3">
          <div className="flex justify-between items-center mb-2">
            <span className="font-medium text-white">{e.name}</span>
            <span className={`px-2 py-0.5 rounded text-xs text-white ${STATUS_STYLE[e.status]}`}>{e.status}</span>
          </div>
          <div className="text-2xl font-mono text-white">
            {e.current_temp.toFixed(1)}°{e.unit}
          </div>
          {/* semgrep: English-only demo UI; strings are intentionally not internationalized. */}
          {/* nosemgrep: jsx-not-internationalized */}
          <div className="text-xs text-gray-400">Target: {e.target_temp}°{e.unit}</div>
          {e.active_alert && (
            <div className="mt-2 text-xs text-red-400 bg-red-900/30 rounded p-1">
              ⚠ {e.active_alert.type.replace(/_/g, ' ')}
            </div>
          )}
          {e.timer_remaining_seconds != null && (
            <div className="mt-1 text-xs text-blue-400">⏱ {Math.ceil(e.timer_remaining_seconds / 60)}min remaining</div>
          )}
        </div>
      ))}
    </div>
  )
}
