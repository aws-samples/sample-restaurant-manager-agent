// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

export function StaffPanel({ schedule }) {
  if (!schedule) return null
  const employees = schedule.employees || []
  const lc = schedule.labor_cost || {}

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-3 gap-3">
        {[
          ['Labor Cost', `$${(lc.total_labor_cost ?? 0).toFixed(2)}`],
          ['% of Revenue', `${(lc.labor_cost_percentage ?? 0).toFixed(1)}%`],
          ['On Clock', lc.employees_on_clock ?? 0],
        ].map(([l, v]) => (
          <div key={l} className="bg-gray-700 rounded p-2 text-center">
            <div className="text-lg font-bold text-white">{v}</div>
            <div className="text-xs text-gray-400">{l}</div>
          </div>
        ))}
      </div>

      <div className="space-y-1">
        {employees.map(e => (
          <div key={e.id} className="flex items-center gap-3 bg-gray-700 rounded p-2">
            <div className={`w-2 h-2 rounded-full ${e.is_clocked_in ? 'bg-green-400' : 'bg-gray-500'}`} />
            <span className="text-white font-medium w-36">{e.name}</span>
            <span className="text-gray-400 text-sm w-20">{e.role.replace(/_/g, ' ')}</span>
            <span className="text-gray-300 text-sm">{e.hours_today.toFixed(1)}h</span>
            <span className="text-green-400 text-sm ml-auto">${e.labor_cost_today.toFixed(2)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
