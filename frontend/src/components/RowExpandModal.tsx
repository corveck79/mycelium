import { createPortal } from 'react-dom';
import { useInfiniteQuery } from '@tanstack/react-query';
import type { TmdbItem } from '../types';
import PosterCard from './PosterCard';

export default function RowExpandModal({
  title,
  queryKey,
  fetchPage,
  onClose,
  onItemClick,
}: {
  title: string;
  queryKey: unknown[];
  fetchPage: (page: number) => Promise<TmdbItem[]>;
  onClose: () => void;
  onItemClick: (item: TmdbItem) => void;
}) {
  const { data, isLoading, isFetchingNextPage, fetchNextPage, hasNextPage } = useInfiniteQuery({
    queryKey,
    queryFn: ({ pageParam }) => fetchPage(pageParam),
    initialPageParam: 1,
    getNextPageParam: (lastPage, allPages) => (lastPage.length > 0 ? allPages.length + 1 : undefined),
  });

  const items = data?.pages.flat() || [];

  return createPortal(
    <div
      className="fixed inset-0 z-[200] bg-black/90 backdrop-blur-sm overflow-y-auto p-4 sm:p-8"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="max-w-6xl mx-auto">
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-xl font-bold">{title}</h2>
          <button
            type="button"
            onClick={onClose}
            className="w-9 h-9 rounded-full bg-black/40 hover:bg-black/60 text-white text-xl flex items-center justify-center"
            aria-label="Close"
          >
            ×
          </button>
        </div>
        {isLoading ? (
          <div className="text-muted text-sm py-6">Loading...</div>
        ) : items.length === 0 ? (
          <div className="text-muted text-sm py-6">Nothing to show.</div>
        ) : (
          <>
            <div
              className="grid gap-3"
              style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(140px, 200px))' }}
            >
              {items.map((item) => (
                <PosterCard
                  key={`${item.media_type}-${item.tmdb_id}`}
                  item={item}
                  onClick={onItemClick}
                  status={item.library_status}
                />
              ))}
            </div>
            {hasNextPage && (
              <div className="flex justify-center mt-6">
                <button
                  type="button"
                  onClick={() => fetchNextPage()}
                  disabled={isFetchingNextPage}
                  className="px-4 py-2 rounded-lg border border-border hover:border-accent/50 text-sm disabled:opacity-60"
                >
                  {isFetchingNextPage ? 'Loading...' : 'Load more'}
                </button>
              </div>
            )}
          </>
        )}
      </div>
    </div>,
    document.body,
  );
}
