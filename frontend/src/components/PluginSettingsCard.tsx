/**
 * Generic renderer for plugin settings UI descriptors.
 * Plugins declare their settings in PLUGIN_META["settings_ui"] (Python).
 * No frontend changes needed when adding a new plugin  -  just define the descriptor.
 */
import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { PluginMeta, PluginSettingsUi } from '../hooks/usePlugins'
import { api, csrfToken } from '../api'

export default function PluginSettingsCard({ plugin, embedded }: { plugin: PluginMeta; embedded?: boolean }) {
  const ui = plugin.settings_ui!
  const qc = useQueryClient()

  const { data: session } = useQuery({ queryKey: ['session'], queryFn: api.session })
  const isAdmin = session?.user?.role === 'admin'

  const { data: status, refetch: refetchStatus } = useQuery<Record<string, any>>({
    queryKey: ['plugin-status', plugin.name],
    queryFn:  () => fetch(ui.status_url, { credentials: 'same-origin' }).then(r => {
      if (!r.ok) throw new Error(`${r.status}`)
      return r.json()
    }),
  })

  const configured = !ui.config_gate || !!status?.[ui.config_gate.field]
  const oAuth = ui.oauth_device
  const connected = oAuth ? !!status?.[oAuth.connected_field] : null
  const username = oAuth?.username_field ? status?.[oAuth.username_field] : null
  const syncedAt = oAuth?.synced_field ? status?.[oAuth.synced_field] : null

  const onConnectChange = () => {
    refetchStatus()
    qc.invalidateQueries({ queryKey: ['session'] })
  }

  const inner = (
    <>
      {ui.config_gate && !configured && status !== undefined && (
        isAdmin ? (
          <ConfigGateAlert
            message={ui.config_gate.message}
            link={ui.config_gate.link}
            linkLabel={ui.config_gate.link_label}
          />
        ) : (
          <ConfigGateAlert
            message={`${plugin.label} is not configured yet. Ask your admin to enable it.`}
          />
        )
      )}

      {oAuth && (
        <OAuthDeviceSection
          spec={oAuth}
          configured={configured}
          connected={!!connected}
          username={username}
          syncedAt={syncedAt}
          onStatusChange={onConnectChange}
        />
      )}

      {ui.actions?.map(action => {
        const show = !action.show_if || !!status?.[action.show_if]
        if (!show) return null
        return (
          <ActionButton
            key={action.label}
            action={action}
            onSuccess={() => {
              qc.invalidateQueries({ queryKey: ['trakt-watched'] })
              qc.invalidateQueries({ queryKey: ['watchlist'] })
            }}
          />
        )
      })}
    </>
  )

  if (embedded) return inner

  return (
    <div className="bg-card rounded-lg border border-border p-6">
      <div className="flex items-center gap-3 mb-4">
        <div>
          <h2 className="text-base font-bold leading-tight">{plugin.label}</h2>
          <p className="text-muted text-xs">{plugin.description}</p>
        </div>
      </div>
      {inner}
    </div>
  )
}

// ── Config gate ────────────────────────────────────────────────────────────────

function ConfigGateAlert({ message, link, linkLabel }: {
  message: string; link?: string; linkLabel?: string
}) {
  return (
    <div className="text-xs text-yellow-400 bg-yellow-400/10 border border-yellow-400/20 rounded px-3 py-2 mb-4">
      {message}
      {link && (
        <>
          {' '}
          <a href={link} target="_blank" rel="noopener" className="underline">
            {linkLabel || link}
          </a>
        </>
      )}
    </div>
  )
}

// ── OAuth device flow ──────────────────────────────────────────────────────────

function OAuthDeviceSection({ spec, configured, connected, username, syncedAt, onStatusChange }: {
  spec:           NonNullable<PluginSettingsUi['oauth_device']>
  configured:     boolean
  connected:      boolean
  username:       string | null
  syncedAt:       string | null
  onStatusChange: () => void
}) {
  const [phase, setPhase] = useState<'idle' | 'waiting' | 'done'>('idle')
  const [deviceInfo, setDeviceInfo] = useState<{
    user_code: string; verification_url: string; interval: number
  } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval>>()

  const startAuth = async () => {
    setError(null)
    try {
      const r = await fetch(spec.start_url, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json',
                   'X-CSRFToken': csrfToken() },
      })
      if (!r.ok) {
        const j = await r.json().catch(() => ({}))
        throw new Error(j.error || `${r.status}`)
      }
      const data = await r.json()
      setDeviceInfo({ user_code: data.user_code, verification_url: data.verification_url, interval: data.interval })
      setPhase('waiting')
    } catch (e: any) {
      setError(e.message)
    }
  }

  useEffect(() => {
    if (phase !== 'waiting' || !deviceInfo) return
    const ms = Math.max(deviceInfo.interval * 1000, 5000)
    pollRef.current = setInterval(async () => {
      try {
        const r = await fetch(spec.poll_url, { credentials: 'same-origin' })
        if (r.status === 401) {
          // Session expired mid-poll - match api.ts's http() wrapper instead
          // of silently polling a dead session (the same bug already fixed
          // in DetailModal.tsx's request-status poll).
          clearInterval(pollRef.current)
          setPhase('idle')
          setDeviceInfo(null)
          if (!window.location.pathname.endsWith('/login')) window.location.href = '/login'
          return
        }
        const data = await r.json()
        if (data.status === 'connected') {
          clearInterval(pollRef.current)
          setPhase('idle')
          setDeviceInfo(null)
          onStatusChange()
        } else if (data.status === 'error' || data.status === 'expired') {
          clearInterval(pollRef.current)
          setPhase('idle')
          setDeviceInfo(null)
          setError(data.error || `Auth ${data.status}`)
        }
      } catch { /* keep polling on network hiccup */ }
    }, ms)
    return () => clearInterval(pollRef.current)
  }, [phase, deviceInfo])

  const revokeAuth = async () => {
    await fetch(spec.revoke_url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'X-CSRFToken': csrfToken() },
    })
    onStatusChange()
  }

  if (connected) {
    return (
      <div className="space-y-2 mb-4">
        <p className="text-sm">
          Connected as <span className="font-semibold text-white">{username}</span>
        </p>
        {syncedAt && <p className="text-xs text-muted">Last synced: {syncedAt}</p>}
        <button
          onClick={revokeAuth}
          className="px-3 py-1.5 rounded bg-red-500/20 text-red-400 text-xs font-medium hover:bg-red-500/30"
        >
          Disconnect
        </button>
      </div>
    )
  }

  if (phase === 'waiting' && deviceInfo) {
    return (
      <div className="space-y-3 mb-4">
        <p className="text-sm text-muted">
          Go to{' '}
          <a href={deviceInfo.verification_url} target="_blank" rel="noopener"
             className="text-accent underline font-medium">
            {deviceInfo.verification_url}
          </a>{' '}
          and enter:
        </p>
        <div className="font-mono text-2xl font-bold tracking-widest text-white bg-zinc-800 px-4 py-3 rounded-lg inline-block">
          {deviceInfo.user_code}
        </div>
        <p className="text-xs text-muted animate-pulse">Waiting for confirmation…</p>
        <button
          onClick={() => { clearInterval(pollRef.current); setPhase('idle'); setDeviceInfo(null) }}
          className="text-xs text-muted hover:text-white"
        >
          Cancel
        </button>
      </div>
    )
  }

  return (
    <div className="space-y-2 mb-4">
      {error && <p className="text-xs text-red-400">{error}</p>}
      <button
        onClick={startAuth}
        disabled={!configured}
        className="px-4 py-2 rounded bg-accent text-sm font-semibold hover:bg-accent/90
                   disabled:opacity-40 disabled:cursor-not-allowed"
      >
        Connect account
      </button>
    </div>
  )
}

// ── Action button ──────────────────────────────────────────────────────────────

function ActionButton({ action, onSuccess }: {
  action: NonNullable<PluginSettingsUi['actions']>[number]
  onSuccess?: () => void
}) {
  const [result, setResult] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const run = async () => {
    setLoading(true)
    setResult(null)
    try {
      const r = await fetch(action.url, {
        method: action.method || 'POST',
        credentials: 'same-origin',
        headers: { 'X-CSRFToken': csrfToken() },
      })
      const data = await r.json()
      if (action.success_template && action.success_key != null) {
        setResult(action.success_template.replace(`{${action.success_key}}`, data[action.success_key] ?? ''))
      }
      onSuccess?.()
    } catch (e: any) {
      setResult(`Error: ${e.message}`)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex items-center gap-2 flex-wrap">
      <button
        onClick={run}
        disabled={loading}
        className="px-3 py-1.5 rounded bg-accent/20 text-accent text-xs font-medium
                   hover:bg-accent/30 disabled:opacity-50"
      >
        {loading ? 'Working…' : action.label}
      </button>
      {result && <span className="text-xs text-ok">{result}</span>}
    </div>
  )
}
