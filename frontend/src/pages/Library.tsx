import { useState, lazy, Suspense } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api';

const PlayerModal = lazy(() => import('../components/PlayerModal'));

type Tab = 'movies' | 'series';

export default function Library() {
  const [tab, setTab] = useState<Tab>('movies');
  return (
    <div>
      <div className="flex gap-2 border-b border-border mb-5">
        {(['movies', 'series'] as Tab[]).map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTab(t)}
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px capitalize transition ${
              tab === t ? 'border-accent text-white' : 'border-transparent text-muted hover:text-white'
            }`}
          >
            {t}
          </button>
        ))}
      </div>
      {tab === 'movies' ? <MoviesPanel /> : <SeriesPanel />}
    </div>
  );
}

function MoviesPanel() {
  const { data, isLoading } = useQuery({
    queryKey: ['library-movies'],
    queryFn: api.libraryMovies,
  });
  if (isLoading) return <div className="text-muted">Loading...</div>;
  const items = data?.items || [];
  const available = items.filter((m: any) => m.status === 'success');
  const wanted = items.filter((m: any) => m.status === 'wanted');
  const upcoming = items.filter((m: any) => m.status === 'upcoming' || m.status === 'failed');
  return (
    <div className="space-y-6">
      <MovieTable title="Available" items={available} />
      {wanted.length > 0 && <MovieTable title="Wanted" items={wanted} dimmed />}
      {upcoming.length > 0 && <MovieTable title="Upcoming" items={upcoming} dimmed />}
    </div>
  );
}

function MovieTable({ title, items, dimmed }: { title: string; items: any[]; dimmed?: boolean }) {
  if (items.length === 0) return null;
  return (
    <div className={dimmed ? 'opacity-60' : ''}>
      <p className="text-xs uppercase tracking-wider text-muted font-semibold mb-2">
        {title} <span className="text-accent">{items.length}</span>
      </p>
      <div className="rounded-xl border border-border overflow-hidden">
        <table className="w-full text-sm">
          <thead className="text-xs text-muted uppercase border-b border-border bg-card">
            <tr>
              <th className="text-left py-2 px-3">Title</th>
              <th className="text-left py-2 px-3">Quality</th>
              <th className="text-left py-2 px-3">Source</th>
              <th className="text-left py-2 px-3">Added</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {items.map((m: any) => (
              <tr key={m.imdb_id} className="hover:bg-card/50 transition">
                <td className="py-2 px-3 font-medium">
                  <div>{m.title}</div>
                  <div className="text-[10px] text-muted font-mono">{m.imdb_id}</div>
                </td>
                <td className="py-2 px-3 text-muted">{m.quality || '-'}</td>
                <td className="py-2 px-3 text-muted text-xs">{m.source || '-'}</td>
                <td className="py-2 px-3 text-muted text-xs">{fmtDate(m.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    success: 'bg-green-500/20 text-green-400',
    processing: 'bg-yellow-500/20 text-yellow-400',
    failed: 'bg-red-500/20 text-red-400',
    wanted: 'bg-orange-500/20 text-orange-400',
    upcoming: 'bg-blue-500/20 text-blue-400',
    rate_limited: 'bg-red-500/20 text-red-400',
  };
  return (
    <span className={`text-xs px-2 py-0.5 rounded ${colors[status] || 'bg-card text-muted'}`}>
      {(status || 'pending').replace('_', ' ')}
    </span>
  );
}

function SeriesPanel() {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const { data, isLoading } = useQuery({
    queryKey: ['library-series-episodes'],
    queryFn: () => fetch('/ui/api/library/series-episodes').then(r => r.json()),
  });
  const { data: session } = useQuery({ queryKey: ['session'], queryFn: api.session });
  // webplayer_enabled is only present when the webplayer plugin is loaded
  const canPlay = !!(session?.user as any)?.webplayer_enabled;
  const [playEp, setPlayEp] = useState<{
    imdb_id: string; season: number; episode: number; title: string
  } | null>(null);

  if (isLoading) return <div className="text-muted">Loading...</div>;
  const series: any[] = data?.series || [];

  const toggle = (title: string) => {
    setExpanded(prev => {
      const next = new Set(prev);
      next.has(title) ? next.delete(title) : next.add(title);
      return next;
    });
  };

  return (
    <>
    <div>
      <p className="text-muted text-sm mb-4">{series.length} series in library</p>
      <div className="space-y-1">
        {series.map((s: any) => {
          const isOpen = expanded.has(s.title);
          const totalEps = s.seasons.reduce((n: number, se: any) => n + se.episodes.length, 0);
          const missingList: {season: number; episode: number}[] = s.missing || [];
          const missingCount = missingList.length;
          const missingSet = new Set(missingList.map((m: any) => `${m.season}-${m.episode}`));
          return (
            <div key={s.title} className="border border-border rounded">
              <button
                type="button"
                onClick={() => toggle(s.title)}
                className="w-full flex items-center justify-between px-4 py-3 text-sm hover:bg-card transition text-left"
              >
                <span className="font-medium">{s.title}</span>
                <span className="text-muted text-xs">
                  {s.seasons.length} season{s.seasons.length !== 1 ? 's' : ''} · {totalEps} episodes
                  {missingCount > 0 && (
                    <span className="text-red-400 ml-2">{missingCount} missing</span>
                  )}
                  <span className="ml-2">{isOpen ? '▲' : '▼'}</span>
                </span>
              </button>
              {isOpen && (
                <div className="border-t border-border px-4 py-3 space-y-2 bg-card/50">
                  {s.seasons.map((se: any) => {
                    const seasonMissing = missingList
                      .filter((m: any) => m.season === se.season)
                      .map((m: any) => m.episode);
                    const allEps = new Set([...se.episodes, ...seasonMissing]);
                    const sorted = Array.from(allEps).sort((a, b) => a - b);
                    return (
                      <div key={se.season}>
                        <div className="text-xs text-muted mb-1">
                          Season {String(se.season).padStart(2, '0')}{se.year ? ` (${se.year})` : ''} -- {se.episodes.length} episode{se.episodes.length !== 1 ? 's' : ''}
                          {seasonMissing.length > 0 && (
                            <span className="text-red-400 ml-1">({seasonMissing.length} missing)</span>
                          )}
                        </div>
                        <div className="flex flex-wrap gap-1">
                          {sorted.map((ep: number) => {
                            const isWanted = missingSet.has(`${se.season}-${ep}`);
                            const playable = !isWanted && canPlay && s.imdb_id;
                            return playable ? (
                              <button
                                key={ep}
                                type="button"
                                onClick={() => setPlayEp({
                                  imdb_id: s.imdb_id,
                                  season: se.season,
                                  episode: ep,
                                  title: `${s.title} S${String(se.season).padStart(2,'0')}E${String(ep).padStart(2,'0')}`,
                                })}
                                className="text-xs px-2 py-0.5 rounded bg-accent/20 text-accent
                                           hover:bg-indigo-600 hover:text-white transition-colors"
                                title="Play in browser"
                              >
                                ▶ E{String(ep).padStart(2, '0')}
                              </button>
                            ) : (
                              <span
                                key={ep}
                                className={`text-xs px-2 py-0.5 rounded ${
                                  isWanted
                                    ? 'bg-red-500/20 text-red-400'
                                    : 'bg-accent/20 text-accent'
                                }`}
                                title={isWanted ? 'Wanted - not yet cached' : 'Available'}
                              >
                                E{String(ep).padStart(2, '0')}
                              </span>
                            );
                          })}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>

    {playEp && (
      <Suspense fallback={null}>
        <PlayerModal
          imdb_id={playEp.imdb_id}
          media_type="tv"
          title={playEp.title}
          season={playEp.season}
          episode={playEp.episode}
          onClose={() => setPlayEp(null)}
        />
      </Suspense>
    )}
    </>
  );
}

function fmtDate(iso: string | null) {
  if (!iso) return '-';
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return iso;
  }
}
