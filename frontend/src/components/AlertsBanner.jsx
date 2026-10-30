// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// Persistent proactive-alert banner (spec 023 Task 6/9). Unacknowledged alerts
// surface here unsolicited on the always-on display, each with an Acknowledge
// action that publishes the ack upstream (resuming the cloud workflow).
const SEV_STYLE = {
  critical: 'bg-red-700 border-red-400',
  warning: 'bg-yellow-700 border-yellow-400',
  info: 'bg-blue-700 border-blue-400',
}

export function AlertsBanner({ alerts, onAck }) {
  const active = (alerts?.entries || []).filter(a => !a.acknowledged)
  if (active.length === 0) return null

  return (
    <div className="mb-4 space-y-2" data-testid="alerts-banner">
      {active.slice().reverse().map(a => (
        <div
          key={a.alert_id}
          className={`flex items-center justify-between rounded-lg border-l-4 px-4 py-3 text-white ${SEV_STYLE[a.severity] || SEV_STYLE.info}`}
        >
          <div className="flex items-center gap-3">
            <span className="text-xl">🔔</span>
            <div>
              <div className="font-semibold">{a.message || `${a.severity || 'Alert'}`}</div>
              <div className="text-xs opacity-80">
                {a.source_id ? `${a.source_id} · ` : ''}
                {(a.condition || '').replace(/_/g, ' ')}
                {a.timestamp ? ` · ${a.timestamp}` : ''}
              </div>
            </div>
          </div>
          {/* semgrep: English-only demo UI; strings are intentionally not internationalized. */}
          {/* nosemgrep: jsx-not-internationalized */}
          <button
            onClick={() => onAck(a.alert_id)}
            className="ml-4 shrink-0 rounded bg-white/90 px-3 py-1 text-sm font-medium text-gray-900 hover:bg-white"
          >
            Acknowledge
          </button>
        </div>
      ))}
    </div>
  )
}
