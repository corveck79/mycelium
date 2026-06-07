import { useEffect, useRef, useState, useCallback } from 'react'
import Hls from 'hls.js'
import { useQuery, useMutation } from '@tanstack/react-query'
import SubtitlePicker from './SubtitlePicker'
import { api } from '../../api'

const csrfToken = () =>
  document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content || ''

const STEP_LABELS: Record<string, string> = {
  searching:     'Looking for a web-compatible version…',
  materializing: 'Fetching via TorBox…',
  probing:       'Reading file info…',
  preparing:     'Preparing for playback…',
  ready:         'Ready',
}

const STEPS = ['searching', 'materializing', 'probing', 'preparing'] as const

interface AudioTrack { index: number; codec: string; language: string; title: string }
interface FileInfo {
  duration_s:    number
  video_codec:   string
  width:         number
  height:        number
  is_hdr:        boolean
  audio_tracks:  AudioTrack[]
  subtitle_tracks: any[]
}

interface JobStatus {
  status:       'searching' | 'materializing' | 'probing' | 'preparing' | 'ready' | 'error'
  message:      string
  token?:       string
  stream_url?:  string
  stream_type?: 'direct' | 'hls'
  cdn_url?:     string
  file_info?:   FileInfo
  error?:       string
}

export default function PlayerModal({ imdb_id, media_type, title, season, episode, onClose }: {
  imdb_id:    string
  media_type: string
  title:      string
  season?:    number
  episode?:   number
  onClose:    () => void
}) {
  const videoRef   = useRef<HTMLVideoElement>(null)
  const hlsRef     = useRef<Hls | null>(null)
  const saveTimer  = useRef<ReturnType<typeof setInterval>>()
  const { data: session } = useQuery({ queryKey: ['session'], queryFn: api.session })
  // trakt_connected is only present when the trakt plugin is loaded
  const traktEnabled = !!(session?.user as any)?.trakt_connected

  const [jobId,       setJobId]       = useState<string | null>(null)
  const [subtitleUrl, setSubtitleUrl] = useState<string | null>(null)
  const [jumpMin,     setJumpMin]     = useState('')
  const [jumping,     setJumping]     = useState(false)

  const prepareMutation = useMutation({
    mutationFn: () =>
      fetch('/ui/api/web-player/prepare', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
        body: JSON.stringify({ imdb_id, media_type, season, episode }),
      }).then(r => r.json()) as Promise<{ job_id: string }>,
    onSuccess: r => setJobId(r.job_id),
  })

  useEffect(() => { prepareMutation.mutate() }, [])

  const { data: status } = useQuery<JobStatus>({
    queryKey: ['wp-status', jobId],
    queryFn:  () => fetch(`/ui/api/web-player/status/${jobId}`).then(r => r.json()),
    enabled:  !!jobId,
    refetchInterval: q =>
      q.state.data?.status === 'ready' || q.state.data?.status === 'error' ? false : 800,
  })

  useEffect(() => {
    if (status?.status !== 'ready' || !status.stream_url || !videoRef.current) return

    const video    = videoRef.current
    const isDirect = status.stream_type === 'direct'

    if (isDirect) {
      video.src = status.stream_url!
    } else if (Hls.isSupported()) {
      const hls = new Hls({ enableWorker: false })
      hls.on(Hls.Events.ERROR, (_e, data) => {
        if (!data.fatal) return
        console.error('HLS fatal error:', data.type, data.details, data)
        if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
          hls.recoverMediaError()
        } else {
          hls.destroy()
        }
      })
      hls.attachMedia(video)
      hls.loadSource(status.stream_url!)
      hlsRef.current = hls
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      // Safari: native HLS
      video.src = status.stream_url
    } else {
      video.src = status.stream_url
    }

    video.addEventListener('loadedmetadata', () => video.play(), { once: true })

    video.addEventListener('play', () => {
      if (traktEnabled) {
        const progress = video.duration ? (video.currentTime / video.duration) * 100 : 0
        api.traktScrobble({ action: 'start', media_type, imdb_id, progress, season, episode, title })
      }
    }, { once: true })

    saveTimer.current = setInterval(() => {
      if (sessionKey && !video.paused) {
        fetch(`/stream/${sessionKey}/position`, {
          method:  'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
          body:    JSON.stringify({ position_s: video.currentTime, duration_s: video.duration }),
        })
      }
    }, 10_000)

    return () => {
      if (traktEnabled && video.duration) {
        const progress = (video.currentTime / video.duration) * 100
        const action = progress >= 80 ? 'stop' : 'pause'
        api.traktScrobble({ action, media_type, imdb_id, progress, season, episode, title })
      }
      hlsRef.current?.destroy()
      clearInterval(saveTimer.current)
    }
  }, [status?.status])

  // Reload Hls.js (or native video) with a new source URL  -  used after seek restart.
  const reloadHls = useCallback((url: string) => {
    const video = videoRef.current
    if (!video) return
    if (hlsRef.current) {
      hlsRef.current.loadSource(url)
      hlsRef.current.startLoad()
    } else {
      video.src = url
      video.load()
      video.play().catch(() => {})
    }
  }, [])

  const handleJump = async () => {
    if (!sessionKey) return
    const mins = parseFloat(jumpMin)
    if (isNaN(mins) || mins < 0) return
    const position_s = mins * 60
    setJumping(true)
    try {
      const r = await fetch(`/stream/${sessionKey}/seek`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
        body:    JSON.stringify({ position_s }),
      })
      if (r.ok) {
        const { stream_url } = await r.json()
        reloadHls(stream_url)
        if (videoRef.current) videoRef.current.currentTime = position_s
      }
    } finally {
      setJumping(false)
      setJumpMin('')
    }
  }

  useEffect(() => {
    if (!videoRef.current || !subtitleUrl) return
    const video = videoRef.current

    // Disable any previously injected tracks
    Array.from(video.textTracks).forEach(t => {
      if (t.label === 'external') t.mode = 'disabled'
    })
    Array.from(video.querySelectorAll('track[label="external"]')).forEach(t => t.remove())

    fetch(subtitleUrl)
      .then(r => r.text())
      .then(vtt => {
        // addTextTrack creates a live, writable TextTrack  -  works with HLS.js
        const tt = video.addTextTrack('subtitles', 'external', 'und')
        tt.mode = 'showing'

        const toSec = (t: string) => {
          // Strip optional VTT cue settings (e.g. "line:80% align:center")
          const clean = t.trim().split(/\s/)[0].replace(',', '.')
          const parts = clean.split(':').map(Number)
          return parts.length === 3
            ? parts[0] * 3600 + parts[1] * 60 + parts[2]
            : parts[0] * 60 + parts[1]
        }

        const lines = vtt.split('\n')
        let i = 0
        while (i < lines.length) {
          const line = lines[i].trim()
          if (line.includes('-->')) {
            const [startStr, rawEnd] = line.split('-->')
            const endStr = rawEnd
            const start = toSec(startStr)
            const end   = toSec(endStr)
            i++
            const textLines: string[] = []
            while (i < lines.length && lines[i].trim() !== '') {
              textLines.push(lines[i])
              i++
            }
            const text = textLines.join('\n')
            if (text) {
              try { tt.addCue(new VTTCue(start, end, text)) } catch {}
            }
          } else {
            i++
          }
        }
      })
      .catch(err => console.warn('subtitle load failed', err))
  }, [subtitleUrl])

  // Derived after status is declared  -  info_hash is segment [2] of /stream/<hash>/hls/...
  const sessionKey = status?.stream_url?.split('/')[2] ?? null

  const stepIndex = STEPS.indexOf((status?.status ?? 'searching') as any)
  const fileInfo  = status?.file_info

  return (
    <div
      className="fixed inset-0 z-[300] bg-black/90 flex items-center justify-center p-4"
      onClick={e => e.target === e.currentTarget && onClose()}
    >
      <div className="relative w-full max-w-5xl" onClick={e => e.stopPropagation()}>
        <button
          onClick={onClose}
          className="absolute -top-8 right-0 text-white/60 hover:text-white text-sm"
        >
          ✕ Close
        </button>

        {/* Loading */}
        {status?.status !== 'ready' && status?.status !== 'error' && (
          <div className="bg-zinc-900 rounded-xl p-10 text-center">
            <p className="text-white font-medium text-lg mb-1">{title}</p>
            <p className="text-zinc-400 text-sm mb-8">
              {status?.message ?? STEP_LABELS.searching}
            </p>
            <div className="flex gap-2 justify-center">
              {STEPS.map((step, i) => (
                <div
                  key={step}
                  className={`h-1 w-20 rounded-full transition-all duration-300 ${
                    i <= stepIndex ? 'bg-indigo-500' : 'bg-zinc-700'
                  }`}
                />
              ))}
            </div>
          </div>
        )}

        {/* Error */}
        {status?.status === 'error' && (
          <div className="bg-zinc-900 rounded-xl p-10 text-center space-y-3">
            <p className="text-red-400 font-medium">{status.error}</p>
            <p className="text-zinc-500 text-sm">
              Use Jellyfin for full codec support.
            </p>
            <button
              onClick={onClose}
              className="px-4 py-2 bg-zinc-700 hover:bg-zinc-600 text-white text-sm rounded-lg"
            >
              Close
            </button>
          </div>
        )}

        {/* Player */}
        {status?.status === 'ready' && (
          <div className="bg-black rounded-xl overflow-hidden shadow-2xl">
            <video
              ref={videoRef}
              controls
              className="w-full aspect-video bg-black"
              crossOrigin="anonymous"
            />
            {fileInfo && (
              <div className="px-4 py-2 bg-zinc-900 flex items-center gap-3 text-xs text-zinc-400 flex-wrap">
                {fileInfo.height && (
                  <span className="text-white font-medium">{fileInfo.height}p</span>
                )}
                <span>{fileInfo.video_codec?.toUpperCase()}</span>

                {fileInfo.audio_tracks?.length > 1 && status?.stream_type !== 'direct' && (
                  <select
                    onChange={e => {
                      if (hlsRef.current) hlsRef.current.audioTrack = +e.target.value
                    }}
                    className="bg-zinc-800 text-zinc-300 rounded px-2 py-0.5"
                  >
                    {fileInfo.audio_tracks.map((t, i) => (
                      <option key={i} value={i}>
                        🎵 {t.language.toUpperCase()}{t.title ? `  -  ${t.title}` : ''}
                      </option>
                    ))}
                  </select>
                )}

                {sessionKey && <SubtitlePicker token={sessionKey} onSelect={setSubtitleUrl} />}


                {fileInfo.duration_s > 0 && (
                  <span className="text-zinc-500">
                    {Math.floor(fileInfo.duration_s / 60)} min total
                  </span>
                )}

                {/* Jump-to: lets the user seek to any position even before
                    FFmpeg has generated segments that far. */}
                {sessionKey && (
                  <span className="ml-auto flex items-center gap-1">
                    <span className="text-zinc-600">Jump to:</span>
                    <input
                      type="number" min="0"
                      max={fileInfo.duration_s > 0 ? Math.floor(fileInfo.duration_s / 60) : undefined}
                      placeholder="min"
                      value={jumpMin}
                      onChange={e => setJumpMin(e.target.value)}
                      onKeyDown={e => e.key === 'Enter' && handleJump()}
                      className="w-14 bg-zinc-800 text-zinc-300 rounded px-2 py-0.5 text-xs"
                    />
                    <button
                      onClick={handleJump}
                      disabled={jumping || !jumpMin}
                      title="Jump to this minute (restarts from nearest keyframe)"
                      className="px-2 py-0.5 bg-zinc-700 hover:bg-zinc-600 rounded text-xs disabled:opacity-40"
                    >
                      {jumping ? '…' : '⏭'}
                    </button>
                  </span>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
