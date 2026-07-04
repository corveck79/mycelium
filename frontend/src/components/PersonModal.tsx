import { useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, tmdbImg } from '../api';
import type { TmdbItem } from '../types';

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
  const { data: person, isLoading } = useQuery({
    queryKey: ['person', personId],
    queryFn: () => api.person(personId!),
    enabled: open,
  });

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[70] bg-black/80 backdrop-blur-sm flex items-center justify-center p-4"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="relative w-full max-w-2xl max-h-[85vh] overflow-y-auto bg-card rounded-2xl border border-border shadow-2xl">
        <button
          type="button"
          onClick={onClose}
          className="absolute top-3 right-3 z-10 w-9 h-9 rounded-full bg-black/60 hover:bg-black/80
                     text-white text-xl flex items-center justify-center"
          aria-label="Close"
        >
          x
        </button>

        {isLoading || !person ? (
          <div className="p-8 text-muted text-sm">Loading...</div>
        ) : (
          <div className="p-6">
            <div className="flex gap-4">
              <div className="w-24 h-24 rounded-full bg-bg overflow-hidden flex-shrink-0">
                {person.profile_path && (
                  <img
                    src={tmdbImg.profile(person.profile_path) || undefined}
                    alt={person.name}
                    className="w-full h-full object-cover"
                  />
                )}
              </div>
              <div>
                <h2 className="text-lg font-bold">{person.name}</h2>
                {person.birthday && (
                  <p className="text-xs text-muted mt-0.5">
                    Born {person.birthday}{person.place_of_birth ? ` · ${person.place_of_birth}` : ''}
                  </p>
                )}
              </div>
            </div>

            {person.biography && (
              <p className="text-sm text-muted mt-4 line-clamp-6">{person.biography}</p>
            )}

            {person.filmography.length > 0 && (
              <div className="mt-5">
                <h3 className="text-sm font-semibold mb-2">Known for</h3>
                <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 gap-3">
                  {person.filmography.map((item) => (
                    <button
                      key={`${item.media_type}-${item.tmdb_id}`}
                      type="button"
                      onClick={() => { onSelectItem(item); onClose(); }}
                      className="text-left group"
                    >
                      <div className="aspect-[2/3] rounded-md overflow-hidden bg-bg border border-border
                                      group-hover:border-accent/50 transition">
                        {item.poster_path ? (
                          <img
                            src={tmdbImg.poster(item.poster_path) || undefined}
                            alt={item.title}
                            className="w-full h-full object-cover"
                          />
                        ) : (
                          <div className="text-xs text-muted p-2 text-center">{item.title}</div>
                        )}
                      </div>
                      <div className="text-[11px] mt-1 font-medium leading-tight line-clamp-2">{item.title}</div>
                      {item.character && (
                        <div className="text-[10px] text-muted line-clamp-1">as {item.character}</div>
                      )}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
