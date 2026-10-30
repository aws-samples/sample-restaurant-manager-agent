// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

export function InventoryPanel({ inventory }) {
  if (!inventory) return null
  const items = inventory.items || []

  return (
    <div className="space-y-2">
      {items.map(i => {
        const pct = Math.min(100, (i.current_quantity / i.max_quantity) * 100)
        const low = i.needs_reorder
        return (
          <div key={i.id} className={`bg-gray-700 rounded p-2 ${low ? 'ring-1 ring-red-500' : ''}`}>
            <div className="flex justify-between text-sm mb-1">
              <span className={low ? 'text-red-400 font-medium' : 'text-white'}>{i.name}</span>
              <span className="text-gray-400">{i.current_quantity.toFixed(1)} / {i.max_quantity} {i.unit}</span>
            </div>
            <div className="w-full bg-gray-600 rounded-full h-2">
              <div
                className={`h-2 rounded-full ${low ? 'bg-red-500' : pct < 40 ? 'bg-yellow-500' : 'bg-green-500'}`}
                style={{ width: `${pct}%` }}
              />
            </div>
          </div>
        )
      })}
    </div>
  )
}
