import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, tmdbImg } from '../api';
import type { BlacklistKind, ContentBlacklistItem } from '../types';

const TABS: { key: BlacklistKind; label: string }[] = [
  { key: 'movie', label: 'Movies' },
  { key: 'tv', label: 'Shows' },
  { key: 'person', label: 'Actors' },
];

export default function Blacklist() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ['content-blacklist'],
    queryFn: () => api.contentBlacklist(),
  });

  const removeMut = useMutation({
    mutationFn: ({ kind, tmdb_id }: { kind: BlacklistKind; tmdb_id: number }) =>
      api.contentBlacklistRemove(kind, tmdb_id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['content-blacklist'] });
    },
  });

  const items = data?.items || [];

  if (isLoading) return <div className="text-muted">Loading…</div>;

  return (
    <div className="space-y-8">
      <p className="text-muted text-sm">
        Movies, shows and actors you blacklist here are hidden from search, discover and
        recommendations, and skipped by auto-approve's trending auto-fill.
      </p>
      {items.length === 0 ? (
        <div className="text-center py-12">
          <div className="text-5xl mb-3">🚫</div>
          <h2 className="text-lg font-semibold mb-1">Nothing blacklisted</h2>
          <p className="text-muted text-sm">
            Use the Blacklist button on a movie, show or actor page to add one.
          </p>
        </div>
      ) : (
        TABS.map((tab) => {
          const tabItems = items.filter((it: ContentBlacklistItem) => it.kind === tab.key);
          if (tabItems.length === 0) return null;
          return (
            <section key={tab.key}>
              <h2 className="text-lg font-bold mb-3">
                {tab.label} ({tabItems.length})
              </h2>
              <div className="grid grid-cols-2 sm:grid-cols-4 md:grid-cols-6 lg:grid-cols-8 gap-3">
                {tabItems.map((it: ContentBlacklistItem) => (
                  <BlacklistCard
                    key={`${it.kind}-${it.tmdb_id}`}
                    item={it}
                    onRemove={() => removeMut.mutate({ kind: it.kind, tmdb_id: it.tmdb_id })}
                    removing={removeMut.isPending}
                  />
                ))}
              </div>
            </section>
          );
        })
      )}
    </div>
  );
}

function BlacklistCard({
  item,
  onRemove,
  removing,
}: {
  item: ContentBlacklistItem;
  onRemove: () => void;
  removing: boolean;
}) {
  const img = item.kind === 'person' ? tmdbImg.profile(item.image) : tmdbImg.poster(item.image);
  return (
    <div className="text-left">
      <div className="aspect-[2/3] rounded-md overflow-hidden bg-card border border-border relative">
        {img ? (
          <img src={img} alt={item.title} className="w-full h-full object-cover" />
        ) : (
          <div className="text-xs text-muted p-2 text-center flex items-center justify-center h-full">
            {item.title}
          </div>
        )}
      </div>
      <div className="text-[11px] mt-1 font-semibold leading-tight line-clamp-2">{item.title}</div>
      <button
        type="button"
        onClick={onRemove}
        disabled={removing}
        className="mt-1 w-full text-[10px] px-1.5 py-1 rounded border border-red-600/50 text-red-400
                    hover:bg-red-600/10 disabled:opacity-60"
      >
        Remove
      </button>
    </div>
  );
}
