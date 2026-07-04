import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api';
import type { MediaType, TmdbItem } from '../types';
import PosterCard from '../components/PosterCard';
import DetailModal from '../components/DetailModal';

export default function Search() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [q, setQ] = useState(searchParams.get('q') || '');
  const [typeFilter, setTypeFilter] = useState<'all' | MediaType>('all');

  // Pick up ?q= changes from the topbar search bar (or a shared link) after mount.
  useEffect(() => {
    const urlQ = searchParams.get('q') || '';
    if (urlQ !== q) setQ(urlQ);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  const updateQuery = (value: string) => {
    setQ(value);
    setSearchParams(value ? { q: value } : {}, { replace: true });
  };
  const [detail, setDetail] = useState<{ id: number; type: MediaType } | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ['search', q],
    queryFn: () => api.search(q).then((r) => r.results),
    enabled: q.trim().length > 0,
  });

  const filtered = (data || []).filter((i) =>
    typeFilter === 'all' ? true : i.media_type === typeFilter,
  );

  const open = (it: TmdbItem) => setDetail({ id: it.tmdb_id, type: it.media_type });

  return (
    <div className="space-y-4">
      <input
        type="text"
        autoFocus
        value={q}
        onChange={(e) => updateQuery(e.target.value)}
        placeholder="Search movies and series..."
        className="w-full max-w-xl bg-bg border border-border rounded-lg px-4 py-3 text-sm
                   focus:outline-none focus:border-accent text-white placeholder-muted/60"
      />
      {q.trim() && (
        <p className="text-muted text-xs">{filtered.length} results for &quot;{q}&quot;</p>
      )}
      {q.trim() ? (
        isLoading ? (
          <div className="text-muted text-sm py-6">Loading...</div>
        ) : filtered.length > 0 ? (
          <div className="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(140px, 200px))' }}>
            {filtered.map((it) => (
              <PosterCard
                key={`${it.media_type}-${it.tmdb_id}`}
                item={it}
                onClick={open}
                status={it.library_status}
              />
            ))}
          </div>
        ) : (
          <div className="text-muted text-sm py-6">No results</div>
        )
      ) : (
        <div className="text-muted text-sm py-8 text-center">
          Start typing to search across movies and series.
        </div>
      )}
      <DetailModal
        tmdbId={detail?.id ?? null}
        mediaType={detail?.type ?? null}
        onClose={() => setDetail(null)}
        onSelectItem={open}
      />
    </div>
  );
}
