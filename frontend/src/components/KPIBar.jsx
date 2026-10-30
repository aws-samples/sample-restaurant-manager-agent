// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

export function KPIBar({ state }) {
  if (!state) return null
  const pos = state.pos || {}
  const equip = state.equipment || {}
  const inv = state.inventory || {}
  const sched = state.schedule || {}

  const revenue = pos.daily_sales?.total_revenue ?? 0
  const orderCount = pos.daily_sales?.order_count ?? 0
  const activeOrders = (pos.orders || []).filter(o => !['completed', 'voided'].includes(o.status)).length
  const alerts = (equip.equipment || []).filter(e => e.active_alert && !e.active_alert.acknowledged).length
  const reorder = (inv.items || []).filter(i => i.needs_reorder).length
  const onClock = sched.labor_cost?.employees_on_clock ?? 0

  const kpis = [
    { label: 'Revenue', value: `$${revenue.toFixed(2)}`, color: 'text-green-400' },
    { label: 'Orders', value: orderCount, color: 'text-blue-400' },
    { label: 'Active', value: activeOrders, color: 'text-yellow-400' },
    { label: 'Alerts', value: alerts, color: alerts > 0 ? 'text-red-400' : 'text-gray-400' },
    { label: 'Reorder', value: reorder, color: reorder > 0 ? 'text-orange-400' : 'text-gray-400' },
    { label: 'On Clock', value: onClock, color: 'text-purple-400' },
  ]

  return (
    <div className="flex gap-4 p-3 bg-gray-800 rounded-lg mb-4">
      {kpis.map(k => (
        <div key={k.label} className="flex-1 text-center">
          <div className={`text-xl font-bold ${k.color}`}>{k.value}</div>
          <div className="text-xs text-gray-400">{k.label}</div>
        </div>
      ))}
    </div>
  )
}
