// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

const STATUS_COLORS = {
  placed: 'bg-blue-500', preparing: 'bg-yellow-500', ready: 'bg-green-500',
  served: 'bg-purple-500', completed: 'bg-gray-500', voided: 'bg-red-500',
}

export function OrdersPanel({ pos }) {
  if (!pos) return null
  const orders = pos.orders || []
  const active = orders.filter(o => !['completed', 'voided'].includes(o.status))
  const ds = pos.daily_sales

  return (
    <div className="space-y-4">
      {ds && (
        <div className="grid grid-cols-4 gap-3">
          {[
            ['Revenue', `$${ds.total_revenue.toFixed(2)}`],
            ['Orders', ds.order_count],
            ['Avg Value', `$${ds.avg_order_value.toFixed(2)}`],
            ['Voids', ds.void_count],
          ].map(([l, v]) => (
            <div key={l} className="bg-gray-700 rounded p-2 text-center">
              <div className="text-lg font-bold text-white">{v}</div>
              <div className="text-xs text-gray-400">{l}</div>
            </div>
          ))}
        </div>
      )}

      <div className="text-sm text-gray-400 mb-1">{active.length} active orders</div>
      <div className="space-y-1 max-h-96 overflow-y-auto">
        {active.map(o => (
          <div key={o.id} className="flex items-center gap-3 bg-gray-700 rounded p-2">
            <span className="font-mono text-white">#{o.id}</span>
            <span className={`px-2 py-0.5 rounded text-xs text-white ${STATUS_COLORS[o.status]}`}>{o.status}</span>
            <span className="text-gray-300 text-sm flex-1">{o.items.map(i => i.menu_item_name).join(', ')}</span>
            <span className="text-green-400 font-medium">${o.total.toFixed(2)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
