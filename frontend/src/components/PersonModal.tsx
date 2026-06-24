import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, tmdbImg } from '../api';
import type { FilmographyItem, TmdbItem } from '../types';

export default function PersonModal({
  personId,
  onClose,
  onSelectItem,
}: {
  personId: number | null;
  onClose: () => void;
  onSelectItem: (item: TmdbItem) => void;
}) {
  const open = personId !== null;
  const queryClient = useQueryClient();

  const { data: person, isLoading } = useQuery({
    queryKey: ['person', personId],
    queryFn: () => api.personDetails(personId!),
    enabled: open,
  });

  const blacklistMutation = useMutation({
    mutationFn: () =>
      person!.is_blacklisted
        ? api.contentBlacklistRemove('person', person!.tmdb_id)
        : api.contentBlacklistAdd('person', person!.tmdb_id, person!.name, person!.profile_path),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['person', personId] });
    },
  });

  const favoriteMutation = useMutation({
    mutationFn: () =>
      person!.is_favorite
        ? api.favoriteActorRemove(person!.tmdb_id)
        : api.favoriteActorAdd(person!.tmdb_id, person!.name, person!.profile_path),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['person', personId] });
    },
  });

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;

  const photo = tmdbImg.profile(person?.profile_path);

  return createPortal(
    <div
      className="fixed inset-0 z-[200] bg-black/85 backdrop-blur-sm overflow-y-auto p-4 sm:p-8"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="relative max-w-5xl mx-auto bg-card rounded-2xl overflow-hidden shadow-2xl p-6 sm:p-8">
        <button
          type="button"
          onClick={onClose}
          className="absolute top-3 right-3 z-10 w-9 h-9 rounded-full bg-black/60 hover:bg-black/80
                      text-white text-xl flex items-center justify-center"
          aria-label="Close"
        >
          ×
        </button>
        {isLoading || !person ? (
          <div className="text-muted text-center py-12">Loading…</div>
        ) : (
          <>
            <div className="flex flex-col sm:flex-row gap-6">
              <div className="flex-shrink-0 w-40 sm:w-52 mx-auto sm:mx-0 aspect-[2/3] rounded-lg overflow-hidden bg-bg">
                {photo ? (
                  <img src={photo} alt={person.name} className="w-full h-full object-cover" />
                ) : (
                  <div className="w-full h-full flex items-center justify-center text-muted text-xs p-3">
                    No photo
                  </div>
                )}
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-start justify-between gap-3">
                  <h2 className="text-2xl sm:text-3xl font-bold">{person.name}</h2>
                  <div className="flex-shrink-0 flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => favoriteMutation.mutate()}
                      disabled={favoriteMutation.isPending}
                      title="Auto-request this actor's new movies and shows"
                      className={`px-3 py-1.5 rounded-lg border text-xs disabled:opacity-50 ${
                        person.is_favorite
                          ? 'border-amber-500 text-amber-400 hover:bg-amber-500/10'
                          : 'border-border hover:bg-bg'
                      }`}
                    >
                      {person.is_favorite ? '✓ Favorited' : '⭐ Favorite'}
                    </button>
                    <button
                      type="button"
                      onClick={() => blacklistMutation.mutate()}
                      disabled={blacklistMutation.isPending}
                      title="Stop getting this actor recommended or auto-requested"
                      className={`px-3 py-1.5 rounded-lg border text-xs disabled:opacity-50 ${
                        person.is_blacklisted
                          ? 'border-red-600 text-red-400 hover:bg-red-600/10'
                          : 'border-border hover:bg-bg'
                      }`}
                    >
                      {person.is_blacklisted ? '✓ Blacklisted' : '🚫 Blacklist'}
                    </button>
                  </div>
                </div>
                <div className="flex flex-wrap gap-2 mt-3 text-xs">
                  {person.known_for_department && <Badge>{person.known_for_department}</Badge>}
                  {person.birthday && <Badge>Born {person.birthday}</Badge>}
                  {person.place_of_birth && <Badge>{person.place_of_birth}</Badge>}
                </div>
                <p className="text-sm leading-relaxed mt-4 max-w-3xl line-clamp-[10]">
                  {person.biography || 'No biography available.'}
                </p>
              </div>
            </div>

            {person.filmography.length > 0 && (
              <div className="mt-7">
                <h3 className="text-[10px] uppercase tracking-wider text-muted font-semibold mb-3">
                  Filmography ({person.filmography.length})
                </h3>
                <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-6 gap-3">
                  {person.filmography.map((f: FilmographyItem) => (
                    <FilmographyCard
                      key={`${f.media_type}-${f.tmdb_id}`}
                      item={f}
                      personId={personId}
                      onSelectItem={onSelectItem}
                    />
                  ))}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>,
    document.body,
  );
}

function Badge({ children }: { children: React.ReactNode }) {
  return <span className="bg-bg px-2 py-0.5 rounded text-xs">{children}</span>;
}

const STATUS_STYLES: Record<string, { bg: string; label: string }> = {
  success: { bg: 'bg-green-600', label: 'In library' },
  available: { bg: 'bg-green-600', label: 'In library' },
  wanted: { bg: 'bg-yellow-600', label: 'Wanted' },
  upcoming: { bg: 'bg-blue-600', label: 'Upcoming' },
  pending: { bg: 'bg-yellow-600', label: 'Pending' },
  failed: { bg: 'bg-red-600', label: 'Failed' },
};

function FilmographyCard({
  item,
  personId,
  onSelectItem,
}: {
  item: FilmographyItem;
  personId: number | null;
  onSelectItem: (item: TmdbItem) => void;
}) {
  const queryClient = useQueryClient();
  const [requested, setRequested] = useState(false);

  const addMutation = useMutation({
    mutationFn: () =>
      api.addToLibrary(item.tmdb_id, item.media_type, item.title, {
        genre_ids: item.genre_ids,
        year: item.year,
      }),
    onSuccess: () => {
      setRequested(true);
      queryClient.invalidateQueries({ queryKey: ['person', personId] });
    },
  });

  const status = item.library_status || undefined;
  const statusStyle = status ? STATUS_STYLES[status] : undefined;
  const showRequestButton = !statusStyle && !requested;

  return (
    <div className="text-left">
      <button
        type="button"
        onClick={() => onSelectItem(item)}
        className="block w-full text-left"
      >
        <div className="aspect-[2/3] rounded-md overflow-hidden bg-bg border border-border
                    hover:border-accent/50 transition relative">
          {item.poster_path ? (
            <img
              src={tmdbImg.poster(item.poster_path) || undefined}
              alt={item.title}
              className="w-full h-full object-cover"
            />
          ) : (
            <div className="text-xs text-muted p-2 text-center">{item.title}</div>
          )}
          <div
            className={`absolute top-1 right-1 px-1 py-0.5 rounded text-[9px] font-semibold uppercase ${
              item.media_type === 'tv' ? 'bg-accent/90' : 'bg-black/70'
            } text-white`}
          >
            {item.media_type === 'tv' ? 'TV' : 'Movie'}
          </div>
          {statusStyle && (
            <div className={`absolute top-1 left-1 px-1 py-0.5 rounded text-[9px] font-semibold ${statusStyle.bg} text-white`}>
              {statusStyle.label}
            </div>
          )}
          {requested && !statusStyle && (
            <div className="absolute top-1 left-1 px-1 py-0.5 rounded text-[9px] font-semibold bg-yellow-600 text-white">
              Requested
            </div>
          )}
        </div>
      </button>
      <div className="text-[11px] mt-1 font-semibold leading-tight line-clamp-2">
        {item.title}
      </div>
      <div className="text-[10px] text-muted flex items-center gap-1">
        {item.year && <span>{item.year}</span>}
        {item.character && <span className="truncate">as {item.character}</span>}
      </div>
      {showRequestButton && (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            addMutation.mutate();
          }}
          disabled={addMutation.isPending}
          className="mt-1 w-full text-[10px] px-1.5 py-1 rounded border border-border
                      hover:border-accent/50 text-muted hover:text-white disabled:opacity-60"
        >
          {addMutation.isPending ? 'Requesting...' : addMutation.isError ? 'Retry' : '+ Request'}
        </button>
      )}
    </div>
  );
}
