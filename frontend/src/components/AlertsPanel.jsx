// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// Alerts inbox tab (spec 023 Task 6/9). Full active + recent history, newest
// first; active entries can be acknowledged. Bound to state.alerts.entries,
// which the WebSocket keeps live (snapshot + 'alerts' domain events).
const SEV_BADGE = {
  critical: 'bg-red-600', warning: 'bg-yellow-600', info: 'bg-blue-600',
}

export function AlertsPanel({ alerts, onAck }) {
  const entries = (alerts?.entries || []).slice().reverse() // newest first
  if (entries.length === 0) {
    // semgrep: English-only demo UI; strings are intentionally not internationalized.
    // nosemgrep: jsx-not-internationalized
    return <div className="text-sm text-gray-400">No alerts received.</div>
  }

  return (
    <div className="space-y-2" data-testid="alerts-panel">
      {entries.map(a => (
        <div
          key={a.alert_id}
          className={`flex items-center justify-between rounded p-3 ${a.acknowledged ? 'bg-gray-700/50' : 'bg-gray-700'}`}
        >
          <div className="flex items-center gap-3">
            <span className={`px-2 py-0.5 rounded text-xs text-white ${SEV_BADGE[a.severity] || 'bg-gray-600'}`}>
              {a.severity || 'alert'}
            </span>
            <div>
              <div className={`font-medium ${a.acknowledged ? 'text-gray-400 line-through' : 'text-white'}`}>
                {a.message || (a.condition || '').replace(/_/g, ' ')}
              </div>
              <div className="text-xs text-gray-400">
                {a.source_id}{a.timestamp ? ` · ${a.timestamp}` : ''}
              </div>
            </div>
          </div>
          {a.acknowledged
            ? <span className="shrink-0 text-xs text-green-400">✓ acknowledged</span>
            // nosemgrep: jsx-not-internationalized
            : <button
                onClick={() => onAck(a.alert_id)}
                className="ml-4 shrink-0 rounded bg-blue-600 px-3 py-1 text-sm text-white hover:bg-blue-500"
              >Acknowledge</button>}
        </div>
      ))}
    </div>
  )
}
