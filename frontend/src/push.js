// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// Web Push registration (spec 025 Task 1d; silent re-subscribe on rotation: spec 028).
// Consent-gated: only subscribes after the user explicitly enables notifications.
// The browser holds no AWS credentials; the subscription is POSTed to the store
// backend, which relays it to the cloud registry (the push-sender uses it to
// re-engage a backgrounded display). The in-app inbox remains the delivery floor
// if push is denied.

const STORED_KEY = 'vapidPublicKey' // the app-server key our current subscription was made with

function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4)
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/')
  const raw = atob(base64)
  const out = new Uint8Array(raw.length)
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i)
  return out
}

export function pushSupported() {
  return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window
}

export async function pushState() {
  if (!pushSupported()) return { supported: false }
  const reg = await navigator.serviceWorker.getRegistration()
  const sub = reg ? await reg.pushManager.getSubscription() : null
  return { supported: true, permission: Notification.permission, subscribed: !!sub }
}

async function fetchPublicKey() {
  const resp = await fetch('/api/push/vapid-public-key')
  const { publicKey } = resp.ok ? await resp.json() : {}
  return publicKey || null
}

// Subscribe with the given key and relay to the backend; remember the key on success.
async function subscribeAndRegister(reg, publicKey) {
  await navigator.serviceWorker.ready
  const sub = await reg.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToUint8Array(publicKey),
  })
  const resp = await fetch('/api/push/subscribe', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ subscription: sub.toJSON() }),
  })
  if (resp.ok) localStorage.setItem(STORED_KEY, publicKey)
  return resp.ok
}

export async function enablePush() {
  if (!pushSupported()) return { ok: false, reason: 'unsupported' }
  const reg = await navigator.serviceWorker.register('/sw.js')

  const permission = await Notification.requestPermission()   // explicit consent
  if (permission !== 'granted') return { ok: false, reason: 'permission_denied' }

  const publicKey = await fetchPublicKey()
  if (!publicKey) return { ok: false, reason: 'no_vapid_key' }

  const ok = await subscribeAndRegister(reg, publicKey)
  return { ok, reason: ok ? null : 'register_failed' }
}

// spec 028: after a VAPID key rotation the served public key changes and old
// subscriptions fail with 403 (pruned cloud-side). If the display is already
// subscribed but the served key differs from the key it subscribed with,
// silently unsubscribe + re-subscribe with the new key -- NO re-consent prompt
// (consent was already granted). No-op if unsupported / not consented / not
// subscribed / key unchanged. Safe to call on every app load.
export async function syncPushSubscription() {
  if (!pushSupported()) return { ok: false, reason: 'unsupported' }
  if (Notification.permission !== 'granted') return { ok: false, reason: 'no_consent' }
  const reg = await navigator.serviceWorker.getRegistration()
  if (!reg) return { ok: false, reason: 'no_registration' }
  const sub = await reg.pushManager.getSubscription()
  if (!sub) return { ok: false, reason: 'not_subscribed' } // first-time is enablePush()

  const publicKey = await fetchPublicKey()
  if (!publicKey) return { ok: false, reason: 'no_vapid_key' }
  if (localStorage.getItem(STORED_KEY) === publicKey) return { ok: true, reason: 'unchanged' }

  // Key rotated: drop the stale subscription and re-subscribe with the new key.
  await sub.unsubscribe()
  const ok = await subscribeAndRegister(reg, publicKey)
  return { ok, reason: ok ? 're-subscribed' : 'register_failed' }
}

export async function disablePush() {
  const reg = await navigator.serviceWorker.getRegistration()
  const sub = reg ? await reg.pushManager.getSubscription() : null
  if (sub) await sub.unsubscribe()
  localStorage.removeItem(STORED_KEY)
  return { ok: true }
}
