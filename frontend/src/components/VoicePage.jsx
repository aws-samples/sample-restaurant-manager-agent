// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// Display voice mode (spec 025 Task 1b). Mic capture -> 16kHz PCM -> backend
// voice-broker WS -> Nova Sonic; plays the 24kHz reply, shows transcripts.
// No AWS creds in the browser. Glue: seeds the voice session with the recent
// text-chat history (1e) and forwards proactive alerts to be spoken (1c).
// NOTE: capture uses ScriptProcessor (validated); AudioWorklet is a follow-on.
import { useEffect, useRef, useState } from 'react'
import { enablePush, pushSupported, syncPushSubscription } from '../push'

const WS_PATH = '/ws/voice'
const IN_RATE = 16000
const OUT_RATE = 24000
const ALERT_POLL_MS = 4000

function downsampleTo16k(float32, inRate) {
  if (inRate === IN_RATE) return float32
  const ratio = inRate / IN_RATE
  const outLen = Math.floor(float32.length / ratio)
  const out = new Float32Array(outLen)
  for (let i = 0; i < outLen; i++) out[i] = float32[Math.floor(i * ratio)]
  return out
}

function floatToInt16(float32) {
  const out = new Int16Array(float32.length)
  for (let i = 0; i < float32.length; i++) {
    const s = Math.max(-1, Math.min(1, float32[i]))
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff
  }
  return out
}

// Only natural-language prose is useful (and safe) to seed; skip serialized
// tool-use / message records that messy Memory writes can leave in history,
// so the voice session never vocalizes raw tool calls (spec 025 1e).
function isProseContent(c) {
  if (typeof c !== 'string') return false
  const t = c.trim()
  if (!t || t[0] === '{' || t[0] === '[') return false
  return !/"toolUse"|"toolResult"|"message_id"|"redact_message"/.test(t)
}

async function fetchChatHistory() {
  // 1e: seed cross-modal continuity from the text-chat session, if any.
  try {
    const sid = localStorage.getItem('chat_session_id')
    if (!sid) return []
    const r = await fetch(`/api/chat/history/${encodeURIComponent(sid)}`)
    if (!r.ok) return []
    const d = await r.json()
    return (d.messages || [])
      .filter(m => (m.role === 'user' || m.role === 'assistant') && isProseContent(m.content))
      .map(m => ({ role: m.role, content: m.content }))
  } catch {
    return []
  }
}

export function VoicePage() {
  const [status, setStatus] = useState('idle')
  const [error, setError] = useState(null)
  const [transcript, setTranscript] = useState([])
  const [pushInfo, setPushInfo] = useState('')

  const wsRef = useRef(null)
  const micCtxRef = useRef(null)
  const playCtxRef = useRef(null)
  const streamRef = useRef(null)
  const procRef = useRef(null)
  const nextPlayRef = useRef(0)
  const sourcesRef = useRef([])
  const alertPollRef = useRef(null)
  const seenAlertsRef = useRef(new Set())

  const cleanup = () => {
    if (alertPollRef.current) { clearInterval(alertPollRef.current); alertPollRef.current = null }
    try { procRef.current && procRef.current.disconnect() } catch {}
    try { streamRef.current && streamRef.current.getTracks().forEach(t => t.stop()) } catch {}
    try { micCtxRef.current && micCtxRef.current.close() } catch {}
    try { wsRef.current && wsRef.current.close() } catch {}
    procRef.current = null
    streamRef.current = null
    micCtxRef.current = null
    wsRef.current = null
  }

  useEffect(() => () => cleanup(), [])

  const stopAudioPlayback = () => {
    sourcesRef.current.forEach(s => { try { s.stop() } catch {} })
    sourcesRef.current = []
    if (playCtxRef.current) nextPlayRef.current = playCtxRef.current.currentTime
  }

  const playPcm24k = (arrayBuffer) => {
    if (!playCtxRef.current) playCtxRef.current = new AudioContext()
    const ctx = playCtxRef.current
    const i16 = new Int16Array(arrayBuffer)
    const f32 = new Float32Array(i16.length)
    for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 0x8000
    const buf = ctx.createBuffer(1, f32.length, OUT_RATE)
    buf.getChannelData(0).set(f32)
    const src = ctx.createBufferSource()
    src.buffer = buf
    src.connect(ctx.destination)
    const start = Math.max(nextPlayRef.current, ctx.currentTime)
    src.start(start)
    nextPlayRef.current = start + buf.duration
    sourcesRef.current.push(src)
    src.onended = () => { sourcesRef.current = sourcesRef.current.filter(s => s !== src) }
  }

  // 1c: forward new, unacknowledged proactive alerts to be spoken. Existing
  // alerts at session start are marked seen (already shown in the inbox/banner)
  // so only alerts arriving during the session are announced.
  const startAlertPolling = (ws) => {
    let firstPoll = true
    const poll = async () => {
      if (!ws || ws.readyState !== WebSocket.OPEN) return
      try {
        const r = await fetch('/api/alerts')
        if (!r.ok) return
        const d = await r.json()
        for (const a of (d.alerts || [])) {
          if (a.acknowledged || seenAlertsRef.current.has(a.alert_id)) continue
          seenAlertsRef.current.add(a.alert_id)
          if (!firstPoll) ws.send(JSON.stringify({ type: 'speak_alert', alert: a }))
        }
        firstPoll = false
      } catch { /* transient */ }
    }
    poll()
    alertPollRef.current = setInterval(poll, ALERT_POLL_MS)
  }

  const start = async () => {
    setError(null)
    setTranscript([])
    seenAlertsRef.current = new Set()
    const history = await fetchChatHistory()

    let stream
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } })
    } catch (e) {
      setError(`Microphone permission denied or unavailable (${e.name}). Allow mic access and retry.`)
      setStatus('idle')
      return
    }
    streamRef.current = stream
    setStatus('connecting')

    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${window.location.host}${WS_PATH}`)
    ws.binaryType = 'arraybuffer'
    wsRef.current = ws

    ws.onopen = () => {
      // 1e: send the init handshake (history) BEFORE any audio.
      try { ws.send(JSON.stringify({ type: 'init', history })) } catch {}
      const micCtx = new AudioContext()
      micCtxRef.current = micCtx
      const source = micCtx.createMediaStreamSource(stream)
      const proc = micCtx.createScriptProcessor(4096, 1, 1)
      procRef.current = proc
      proc.onaudioprocess = (ev) => {
        if (ws.readyState !== WebSocket.OPEN) return
        const input = ev.inputBuffer.getChannelData(0)
        const ds = downsampleTo16k(input, micCtx.sampleRate)
        ws.send(floatToInt16(ds).buffer)
      }
      source.connect(proc)
      proc.connect(micCtx.destination)
      startAlertPolling(ws)
    }

    ws.onmessage = (ev) => {
      if (typeof ev.data !== 'string') { playPcm24k(ev.data); return }
      let m
      try { m = JSON.parse(ev.data) } catch { return }
      if (m.type === 'ready') setStatus('listening')
      else if (m.type === 'transcript') {
        setTranscript(prev => [...prev, { role: m.role, text: m.text }])
      } else if (m.type === 'barge_in') stopAudioPlayback()
      else if (m.type === 'error') { setError(m.message) }
    }

    ws.onerror = () => setError('WebSocket error (is VOICE_ENABLED=true on the backend?)')
    ws.onclose = () => { setStatus('idle'); cleanup() }
  }

  const stop = () => {
    try { wsRef.current && wsRef.current.send(JSON.stringify({ type: 'stop' })) } catch {}
    stopAudioPlayback()
    cleanup()
    setStatus('idle')
  }

  // spec 028: on mount, silently re-subscribe if the VAPID key was rotated
  // (the served public key changed). No-op if not subscribed / not consented.
  useEffect(() => {
    syncPushSubscription().catch(() => {})
  }, [])

  const onEnablePush = async () => {
    setPushInfo('requesting permission…')
    try {
      const r = await enablePush()
      setPushInfo(r.ok ? '✓ push alerts enabled on this device' : `not enabled (${r.reason})`)
    } catch (e) {
      setPushInfo(`push error: ${e.message}`)
    }
  }

  return (
    <div className="bg-gray-800 rounded-lg p-4 max-w-3xl">
      {/* semgrep: English-only demo UI; strings are intentionally not internationalized. */}
      {/* nosemgrep: jsx-not-internationalized */}
      <div className="mb-3 rounded bg-yellow-900/40 border border-yellow-700 px-3 py-2 text-xs text-yellow-200">
        {/* nosemgrep: jsx-not-internationalized */}
        Display voice (Nova Sonic). Backend must run with <code>VOICE_ENABLED=true</code>.
        The browser holds no AWS credentials — audio is relayed via the backend. Proactive alerts are
        spoken automatically; ask a question or say "acknowledge the alert".
      </div>
      <div className="flex items-center gap-3 mb-4">
        <button
          onClick={status === 'idle' ? start : stop}
          className={`px-4 py-2 rounded font-medium ${status === 'idle' ? 'bg-green-600 hover:bg-green-500' : 'bg-red-600 hover:bg-red-500'}`}
        >
          {status === 'idle' ? '🎙 Start talking' : '■ Stop'}
        </button>
        {/* nosemgrep: jsx-not-internationalized */}
        <span className="text-sm text-gray-300">status: <span className="font-mono">{status}</span></span>
        {pushSupported() && (
          <button
            onClick={onEnablePush}
            className="ml-auto px-3 py-2 rounded text-sm bg-indigo-600 hover:bg-indigo-500"
          >
            🔔 Enable push alerts
          </button>
        )}
      </div>
      {pushInfo && <div className="mb-3 text-xs text-gray-400">{pushInfo}</div>}
      {error && <div className="mb-3 rounded bg-red-900/50 border border-red-700 px-3 py-2 text-sm text-red-200">{error}</div>}
      <div className="space-y-1">
        {/* nosemgrep: jsx-not-internationalized */}
        {transcript.length === 0 && <div className="text-gray-500 text-sm">Transcript will appear here. Speak after the status shows "listening".</div>}
        {transcript.map((t, i) => (
          <div key={i} className={`text-sm ${t.role === 'assistant' ? 'text-cyan-300' : 'text-gray-300'}`}>
            <span className="font-semibold">{t.role}:</span> {t.text}
          </div>
        ))}
      </div>
    </div>
  )
}
