import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api';
import type { Genre, MediaType, TmdbItem } from '../types';
import PosterGrid from './PosterGrid';
import SectionHeader from './SectionHeader';
import DetailModal from './DetailModal';
import GenreSettingsModal from './GenreSettingsModal';
import RowExpandModal from './RowExpandModal';

export default function GenreBrowser({ mediaType }: { mediaType: MediaType }) {
  const [detail, setDetail] = useState<{ id: number; type: MediaType } | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [expanded, setExpanded] = useState<{ title: string; queryKey: unknown[]; fetchPage: (page: number) => Promise<TmdbItem[]> } | null>(null);
  const open = (item: TmdbItem) => setDetail({ id: item.tmdb_id, type: item.media_type });

  const { data: genresData, isLoading: genresLoading } = useQuery({
    queryKey: ['discover-genres', mediaType],
    queryFn: () => api.genres(mediaType),
  });

  const { data: trending, isLoading: trendingLoading } = useQuery({
    queryKey: ['trending', mediaType, 'week'],
    queryFn: () => api.trending(mediaType, 'week').then((r) => r.results),
  });

  const visibleGenres = genresData?.genres || [];
  const trendingTitle = `🔥 Trending ${mediaType === 'tv' ? 'shows' : 'movies'}`;

  return (
    <div className="space-y-8">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">{mediaType === 'tv' ? 'Shows' : 'Movies'}</h1>
        <button
          type="button"
          onClick={() => setShowSettings(true)}
          className="px-3 py-1.5 rounded-lg border border-border hover:border-accent/50 text-sm flex items-center gap-1.5"
        >
          ⚙️ Genres
        </button>
      </div>

      <section>
        <SectionHeader
          title={trendingTitle}
          action={
            <button
              type="button"
              onClick={() =>
                setExpanded({
                  title: trendingTitle,
                  queryKey: ['trending-all', mediaType, 'week'],
                  fetchPage: (page) => api.trending(mediaType, 'week', page).then((r) => r.results),
                })
              }
              className="text-xs text-muted hover:text-white"
            >
              Show all
            </button>
          }
        />
        <PosterGrid items={trending} loading={trendingLoading} onItemClick={open} />
      </section>

      {genresLoading ? (
        <div className="text-muted text-sm py-6">Loading genres...</div>
      ) : visibleGenres.length === 0 ? (
        <div className="text-muted text-sm py-6">
          No genres to show. Open genre settings to enable some.
        </div>
      ) : (
        visibleGenres.map((g) => (
          <GenreRow
            key={g.id}
            mediaType={mediaType}
            genre={g}
            onItemClick={open}
            onShowAll={() =>
              setExpanded({
                title: g.name,
                queryKey: ['by-genre-all', mediaType, g.id],
                fetchPage: (page) => api.byGenre(mediaType, g.id, page).then((r) => r.results),
              })
            }
          />
        ))
      )}

      <DetailModal
        tmdbId={detail?.id ?? null}
        mediaType={detail?.type ?? null}
        onClose={() => setDetail(null)}
        onSelectItem={open}
      />
      {showSettings && (
        <GenreSettingsModal
          mediaType={mediaType}
          allGenres={genresData?.all_genres || []}
          onClose={() => setShowSettings(false)}
        />
      )}
      {expanded && (
        <RowExpandModal
          title={expanded.title}
          queryKey={expanded.queryKey}
          fetchPage={expanded.fetchPage}
          onClose={() => setExpanded(null)}
          onItemClick={(item) => {
            setExpanded(null);
            open(item);
          }}
        />
      )}
    </div>
  );
}

function GenreRow({
  mediaType,
  genre,
  onItemClick,
  onShowAll,
}: {
  mediaType: MediaType;
  genre: Genre;
  onItemClick: (item: TmdbItem) => void;
  onShowAll: () => void;
}) {
  const { data, isLoading } = useQuery({
    queryKey: ['by-genre', mediaType, genre.id],
    queryFn: () => api.byGenre(mediaType, genre.id).then((r) => r.results),
  });
  if (!isLoading && (!data || data.length === 0)) return null;
  return (
    <section>
      <SectionHeader
        title={genre.name}
        action={
          <button type="button" onClick={onShowAll} className="text-xs text-muted hover:text-white">
            Show all
          </button>
        }
      />
      <PosterGrid items={data} loading={isLoading} onItemClick={onItemClick} />
    </section>
  );
}
