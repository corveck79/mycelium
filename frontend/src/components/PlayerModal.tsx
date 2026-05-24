import { useEffect, useRef, useState } from 'react'
import Hls from 'hls.js'
import { useQuery, useMutation } from '@tanstack/react-query'
import SubtitlePicker from './SubtitlePicker'
import { api } from '../api'

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
  audio_tracks:  AudioTrack[]
  subtitle_tracks: any[]
}

interface JobStatus {
  status:      'searching' | 'materializing' | 'probing' | 'preparing' | 'ready' | 'error'
  message:     string
  token?:      string
  stream_url?: string
  file_info?:  FileInfo
  error?:      string
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
  const [token,       setToken]       = useState<string | null>(null)
  const [subtitleUrl, setSubtitleUrl] = useState<string | null>(null)

  const prepareMutation = useMutation({
    mutationFn: () =>
      fetch('/ui/api/web-player/prepare', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
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
    setToken(status.token ?? null)
    const video = videoRef.current

    if (Hls.isSupported()) {
      const hls = new Hls({ enableWorker: true })
      hls.loadSource(status.stream_url)
      hls.attachMedia(video)
      hlsRef.current = hls
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
      if (token && !video.paused) {
        fetch(`/stream/${token}/position`, {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
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

  useEffect(() => {
    if (!videoRef.current || !subtitleUrl) return
    const track = document.createElement('track')
    track.kind    = 'subtitles'
    track.label   = 'external'
    track.src     = subtitleUrl
    track.default = true
    Array.from(videoRef.current.querySelectorAll('track[label="external"]'))
      .forEach(t => t.remove())
    videoRef.current.appendChild(track)
  }, [subtitleUrl])

  const stepIndex = STEPS.indexOf((status?.status ?? 'searching') as any)
  const fileInfo  = status?.file_info

  return (
    <div
      className="fixed inset-0 z-50 bg-black/90 flex items-center justify-center p-4"
      onClick={e => e.target === e.currentTarget && onClose()}
    >
      <div className="relative w-full max-w-5xl">
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

                {fileInfo.audio_tracks?.length > 1 && (
                  <select
                    onChange={e => {
                      if (hlsRef.current) hlsRef.current.audioTrack = +e.target.value
                    }}
                    className="bg-zinc-800 text-zinc-300 rounded px-2 py-0.5"
                  >
                    {fileInfo.audio_tracks.map((t, i) => (
                      <option key={i} value={i}>
                        🎵 {t.language.toUpperCase()}{t.title ? ` — ${t.title}` : ''}
                      </option>
                    ))}
                  </select>
                )}

                {token && <SubtitlePicker token={token} onSelect={setSubtitleUrl} />}

                {fileInfo.duration_s > 0 && (
                  <span className="ml-auto text-zinc-600">
                    {Math.floor(fileInfo.duration_s / 60)} min
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
