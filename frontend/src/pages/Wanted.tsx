import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '../api';
import type { WantedMovie, WantedEpisode } from '../types';

export default function Wanted() {
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<'movies' | 'episodes'>('movies');

  const { data: moviesData, isLoading: moviesLoading } = useQuery({
    queryKey: ['wanted-movies'],
    queryFn: api.wantedMovies,
    refetchInterval: 30_000,
  });

  const { data: episodesData, isLoading: epsLoading } = useQuery({
    queryKey: ['wanted-episodes'],
    queryFn: api.wantedEpisodes,
    refetchInterval: 30_000,
  });

  const recheckMutation = useMutation({
    mutationFn: api.wantedRecheck,
    onSuccess: () => {
      setTimeout(() => {
        queryClient.invalidateQueries({ queryKey: ['wanted-movies'] });
        queryClient.invalidateQueries({ queryKey: ['wanted-episodes'] });
      }, 3000);
    },
  });

  const movies = moviesData?.items ?? [];
  const episodes = episodesData?.items ?? [];

  const wantedEps = episodes.filter((e) => e.status === 'wanted');
  const notAiredEps = episodes.filter((e) => e.status === 'not_aired');
  const foundEps = episodes.filter((e) => e.status === 'found');

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex gap-1 bg-card rounded-lg p-1">
          <TabBtn active={tab === 'movies'} onClick={() => setTab('movies')}>
            Movies {movies.length > 0 && <Pill>{movies.length}</Pill>}
          </TabBtn>
          <TabBtn active={tab === 'episodes'} onClick={() => setTab('episodes')}>
            Episodes {wantedEps.length > 0 && <Pill>{wantedEps.length}</Pill>}
          </TabBtn>
        </div>
        <button
          type="button"
          onClick={() => recheckMutation.mutate()}
          disabled={recheckMutation.isPending || recheckMutation.isSuccess}
          className="px-4 py-2 rounded-lg bg-accent hover:bg-accent/80 disabled:opacity-60
                     disabled:cursor-not-allowed text-sm font-semibold"
        >
          {recheckMutation.isPending
            ? 'Starting…'
            : recheckMutation.isSuccess
            ? '✓ Recheck running'
            : '↺ Recheck now'}
        </button>
      </div>

      {tab === 'movies' && (
        <section>
          {moviesLoading ? (
            <Spinner />
          ) : movies.length === 0 ? (
            <Empty>No movies on the wanted list.</Empty>
          ) : (
            <div className="rounded-xl border border-border overflow-hidden">
              <table className="w-full text-sm">
                <thead>
                  <tr className="bg-card text-muted text-xs uppercase tracking-wider">
                    <Th>Title</Th>
                    <Th>Reason</Th>
                    <Th>Attempts</Th>
                    <Th>Added</Th>
                    <Th>Last checked</Th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {movies.map((m) => (
                    <MovieRow key={m.imdb_id} movie={m} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}

      {tab === 'episodes' && (
        <section className="space-y-6">
          {epsLoading ? (
            <Spinner />
          ) : (
            <>
              <EpisodesTable
                title="Searching"
                badge={wantedEps.length}
                rows={wantedEps}
                emptyMsg="No episodes being searched."
              />
              <EpisodesTable
                title="Not yet aired"
                badge={notAiredEps.length}
                rows={notAiredEps}
                emptyMsg="No upcoming episodes tracked."
                dimmed
              />
              <EpisodesTable
                title="Found"
                badge={foundEps.length}
                rows={foundEps}
                emptyMsg=""
                dimmed
                collapsed
              />
            </>
          )}
        </section>
      )}
    </div>
  );
}

function MovieRow({ movie }: { movie: WantedMovie }) {
  return (
    <tr className="hover:bg-card/50 transition">
      <td className="px-4 py-3 font-medium">
        <div>{movie.title}</div>
        <div className="text-[10px] text-muted font-mono">{movie.imdb_id}</div>
      </td>
      <td className="px-4 py-3 text-muted text-xs">{movie.reason || '—'}</td>
      <td className="px-4 py-3 text-center">
        <span className="text-xs px-2 py-0.5 rounded bg-bg">{movie.attempts}</span>
      </td>
      <td className="px-4 py-3 text-xs text-muted">{fmtDate(movie.added_at)}</td>
      <td className="px-4 py-3 text-xs text-muted">{movie.last_checked ? fmtDate(movie.last_checked) : '—'}</td>
    </tr>
  );
}

function EpisodesTable({
  title,
  badge,
  rows,
  emptyMsg,
  dimmed = false,
  collapsed = false,
}: {
  title: string;
  badge: number;
  rows: WantedEpisode[];
  emptyMsg: string;
  dimmed?: boolean;
  collapsed?: boolean;
}) {
  const [open, setOpen] = useState(!collapsed);

  if (rows.length === 0 && !emptyMsg) return null;

  return (
    <div className={dimmed ? 'opacity-60' : ''}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-2 mb-2 text-left w-full group"
      >
        <span className="text-xs uppercase tracking-wider text-muted font-semibold group-hover:text-white transition">
          {title}
        </span>
        {badge > 0 && <Pill>{badge}</Pill>}
        <span className="text-muted text-xs ml-auto">{open ? '▲' : '▼'}</span>
      </button>
      {open && (
        <>
          {rows.length === 0 ? (
            <p className="text-sm text-muted">{emptyMsg}</p>
          ) : (
            <div className="rounded-xl border border-border overflow-hidden">
              <table className="w-full text-sm">
                <thead>
                  <tr className="bg-card text-muted text-xs uppercase tracking-wider">
                    <Th>Series</Th>
                    <Th>Episode</Th>
                    <Th>Air date</Th>
                    <Th>Attempts</Th>
                    <Th>Last tried</Th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {rows.map((ep) => (
                    <tr key={ep.id} className="hover:bg-card/50 transition">
                      <td className="px-4 py-3 font-medium">
                        <div>{ep.title}</div>
                        <div className="text-[10px] text-muted font-mono">{ep.imdb_id}</div>
                      </td>
                      <td className="px-4 py-3 font-mono text-xs">
                        S{String(ep.season).padStart(2, '0')}E{String(ep.episode).padStart(2, '0')}
                      </td>
                      <td className="px-4 py-3 text-xs text-muted">{ep.air_date || '—'}</td>
                      <td className="px-4 py-3 text-center">
                        <span className="text-xs px-2 py-0.5 rounded bg-bg">{ep.attempt_count}</span>
                      </td>
                      <td className="px-4 py-3 text-xs text-muted">
                        {ep.last_attempted ? fmtDate(ep.last_attempted) : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function TabBtn({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`px-4 py-1.5 rounded text-sm font-medium flex items-center gap-1.5 transition
        ${active ? 'bg-accent text-white' : 'text-muted hover:text-white'}`}
    >
      {children}
    </button>
  );
}

function Pill({ children }: { children: React.ReactNode }) {
  return (
    <span className="bg-accent/20 text-accent text-[10px] font-bold px-1.5 py-0.5 rounded-full">
      {children}
    </span>
  );
}

function Th({ children }: { children: React.ReactNode }) {
  return <th className="px-4 py-2 text-left font-medium">{children}</th>;
}

function Spinner() {
  return <div className="text-muted text-sm py-8 text-center">Loading…</div>;
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-muted text-sm py-12 text-center bg-card/30 rounded-xl border border-border">
      {children}
    </div>
  );
}

function fmtDate(iso: string) {
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
