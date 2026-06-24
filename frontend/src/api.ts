import type {
  TmdbItem,
  TmdbDetail,
  Provider,
  WatchlistItem,
  UserRecord,
  UserRequest,
  SessionInfo,
  MediaType,
  WantedMovie,
  WantedEpisode,
  Genre,
  DiscoverPrefs,
  AutoApproveRules,
  TmdbPerson,
  PersonDetail,
  Collection,
  BlacklistKind,
  ContentBlacklistItem,
  FavoriteActor,
} from './types';

const csrfToken = (): string => {
  return document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content || '';
};

async function http<T>(url: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method || 'GET').toUpperCase();
  const headers: Record<string, string> = {
    Accept: 'application/json',
    ...(init.headers as Record<string, string> | undefined),
  };
  if (method !== 'GET' && method !== 'HEAD') {
    headers['X-CSRFToken'] = csrfToken();
    if (init.body && !(init.body instanceof FormData)) {
      headers['Content-Type'] = headers['Content-Type'] || 'application/json';
    }
  }
  const resp = await fetch(url, { ...init, headers, credentials: 'same-origin' });
  if (resp.status === 401) {
    if (typeof window !== 'undefined' && !window.location.pathname.endsWith('/login')) {
      window.location.href = '/login';
    }
    throw new Error('unauthorized');
  }
  if (!resp.ok) {
    let detail = '';
    try {
      const j = await resp.json();
      detail = j.error || j.detail || JSON.stringify(j);
    } catch {
      detail = await resp.text();
    }
    throw new Error(`${resp.status}: ${detail}`);
  }
  return (await resp.json()) as T;
}

export const api = {
  // Discovery
  search: (q: string) =>
    http<{ results: TmdbItem[] }>(`/ui/api/discover/search?q=${encodeURIComponent(q)}`),
  trending: (type: 'all' | 'movie' | 'tv' = 'all', window: 'day' | 'week' = 'week', page = 1) =>
    http<{ results: TmdbItem[] }>(`/ui/api/discover/trending?type=${type}&window=${window}&page=${page}`),
  popular: (type: MediaType = 'movie', page = 1) =>
    http<{ results: TmdbItem[] }>(`/ui/api/discover/popular?type=${type}&page=${page}`),
  topRated: (type: MediaType = 'movie', page = 1) =>
    http<{ results: TmdbItem[] }>(`/ui/api/discover/top-rated?type=${type}&page=${page}`),
  nowPlaying: (page = 1) => http<{ results: TmdbItem[] }>(`/ui/api/discover/now-playing?page=${page}`),
  upcoming: (page = 1) => http<{ results: TmdbItem[] }>(`/ui/api/discover/upcoming?page=${page}`),
  onTheAir: (page = 1) => http<{ results: TmdbItem[] }>(`/ui/api/discover/on-the-air?page=${page}`),
  providers: (type: MediaType = 'movie') =>
    http<{ providers: Provider[] }>(`/ui/api/discover/providers?type=${type}`),
  byProvider: (type: MediaType, providerId: number, sortBy?: string) =>
    http<{ results: TmdbItem[] }>(
      `/ui/api/discover/by-provider?type=${type}&provider_id=${providerId}${sortBy ? `&sort_by=${sortBy}` : ''}`,
    ),
  details: (type: MediaType, id: number) =>
    http<TmdbDetail>(`/ui/api/discover/details?type=${type}&id=${id}`),
  addToLibrary: (
    tmdb_id: number,
    media_type: MediaType,
    title: string,
    opts?: {
      monitor_mode?: 'all' | 'future' | 'selected';
      seasons?: number[];
      genre_ids?: number[];
      year?: string | number | null;
    },
  ) =>
    http<{ status: string; request_id?: number; imdb_id?: string; error?: string }>(
      '/ui/api/discover/add',
      {
        method: 'POST',
        body: JSON.stringify({ tmdb_id, media_type, title, ...opts }),
      },
    ),
  addCollection: (collection_id: number) =>
    http<{ status: string; queued: string[]; pending: string[] }>('/ui/api/discover/add-collection', {
      method: 'POST',
      body: JSON.stringify({ collection_id }),
    }),

  // Genre browsing (Shows / Movies tabs)
  genres: (type: MediaType = 'movie') =>
    http<{ genres: Genre[]; all_genres: Genre[] }>(`/ui/api/discover/genres?type=${type}`),
  byGenre: (type: MediaType, genreId: number, page = 1) =>
    http<{ results: TmdbItem[]; year_from: number | null; year_to: number | null }>(
      `/ui/api/discover/by-genre?type=${type}&genre=${genreId}&page=${page}`,
    ),
  holiday: (theme: 'christmas' | 'halloween', page = 1) =>
    http<{ results: TmdbItem[] }>(`/ui/api/discover/holiday?theme=${theme}&page=${page}`),
  discoverPrefsGet: (type: MediaType = 'movie') =>
    http<DiscoverPrefs>(`/ui/api/discover-prefs?type=${type}`),
  discoverPrefsSet: (type: MediaType, prefs: DiscoverPrefs) =>
    http<{ status: string }>('/ui/api/discover-prefs', {
      method: 'POST',
      body: JSON.stringify({ media_type: type, prefs }),
    }),

  // Person search / detail
  searchPerson: (q: string, page = 1) =>
    http<{ results: TmdbPerson[] }>(`/ui/api/discover/search-person?q=${encodeURIComponent(q)}&page=${page}`),
  personDetails: (id: number) =>
    http<PersonDetail>(`/ui/api/discover/person?id=${id}`),

  // Collections (e.g. movie trilogies)
  collectionDetails: (id: number) =>
    http<Collection>(`/ui/api/discover/collection?id=${id}`),

  // Auto-approve rules (per genre/year, Auto-Approve tab)
  autoApproveRulesGet: (type: MediaType = 'movie') =>
    http<{ rules: AutoApproveRules }>(`/ui/api/auto-approve-rules?type=${type}`),
  autoApproveRulesSet: (type: MediaType, rules: AutoApproveRules) =>
    http<{ status: string }>('/ui/api/auto-approve-rules', {
      method: 'POST',
      body: JSON.stringify({ media_type: type, rules }),
    }),
  autoApproveRunNow: () =>
    http<{ status: string }>('/ui/api/auto-approve-rules/run-now', { method: 'POST' }),

  // Content blacklist (movies / shows / actors)
  contentBlacklist: (kind?: BlacklistKind) =>
    http<{ items: ContentBlacklistItem[] }>(
      '/ui/api/content-blacklist' + (kind ? `?kind=${kind}` : ''),
    ),
  contentBlacklistAdd: (kind: BlacklistKind, tmdb_id: number, title: string, image: string | null) =>
    http<{ status: string }>('/ui/api/content-blacklist', {
      method: 'POST',
      body: JSON.stringify({ kind, tmdb_id, title, image }),
    }),
  contentBlacklistRemove: (kind: BlacklistKind, tmdb_id: number) =>
    http<{ status: string }>(`/ui/api/content-blacklist/${kind}/${tmdb_id}`, { method: 'DELETE' }),

  // Favorite actors (auto-requests their recent/upcoming work)
  favoriteActors: () => http<{ items: FavoriteActor[] }>('/ui/api/favorite-actors'),
  favoriteActorAdd: (tmdb_id: number, name: string, profile_path: string | null) =>
    http<{ status: string }>('/ui/api/favorite-actors', {
      method: 'POST',
      body: JSON.stringify({ tmdb_id, name, profile_path }),
    }),
  favoriteActorRemove: (tmdb_id: number) =>
    http<{ status: string }>(`/ui/api/favorite-actors/${tmdb_id}`, { method: 'DELETE' }),

  // Watchlist
  watchlist: () => http<{ items: WatchlistItem[] }>('/ui/api/watchlist'),
  watchlistAdd: (params: {
    imdb_id: string;
    tmdb_id: number | null;
    media_type: MediaType;
    title: string;
    poster_path: string | null;
  }) =>
    http<{ ok: boolean }>('/ui/api/watchlist/add', {
      method: 'POST',
      body: JSON.stringify(params),
    }),
  watchlistRemove: (imdb_id: string, media_type: MediaType) =>
    http<{ ok: boolean }>('/ui/api/watchlist/remove', {
      method: 'POST',
      body: JSON.stringify({ imdb_id, media_type }),
    }),

  // User requests
  userRequests: (status?: string) =>
    http<{ items: UserRequest[] }>(
      '/ui/api/user-requests' + (status ? `?status=${status}` : ''),
    ),
  approveRequest: (id: number) =>
    http<{ ok: boolean }>(`/ui/api/user-requests/${id}/approve`, { method: 'POST' }),
  denyRequest: (id: number, note?: string) =>
    http<{ ok: boolean }>(`/ui/api/user-requests/${id}/deny`, {
      method: 'POST',
      body: JSON.stringify({ note }),
    }),

  // Users (admin)
  users: () => http<{ users: UserRecord[] }>('/ui/api/users'),
  createUser: (params: {
    username: string;
    password: string;
    role?: 'user' | 'admin';
    auto_approve?: boolean;
  }) =>
    http<{ ok: boolean; user_id: number; message?: string }>('/ui/api/users/create', {
      method: 'POST',
      body: JSON.stringify(params),
    }),
  updateUser: (id: number, fields: Partial<UserRecord> & { password?: string }) =>
    http<{ ok: boolean }>(`/ui/api/users/${id}/update`, {
      method: 'POST',
      body: JSON.stringify(fields),
    }),
  deleteUser: (id: number) =>
    http<{ ok: boolean }>(`/ui/api/users/${id}/delete`, { method: 'POST' }),

  // Account
  changePassword: (current: string, password: string) =>
    http<{ ok: boolean; error?: string }>('/ui/api/me/password', {
      method: 'POST',
      body: JSON.stringify({ current, password }),
    }),

  // Plugin user fields (self-service toggle)
  setPluginFields: (fields: Record<string, boolean>) =>
    http<{ ok: boolean }>('/ui/api/me/plugin-fields', {
      method: 'POST',
      body: JSON.stringify(fields),
    }),

  // Region
  setRegion: (region: string) =>
    http<{ ok: boolean; region: string }>('/ui/api/me/region', {
      method: 'POST',
      body: JSON.stringify({ region }),
    }),

  // User preferences
  setPreferences: (prefs: Record<string, boolean>) =>
    http<{ ok: boolean }>('/ui/api/me/preferences', {
      method: 'POST',
      body: JSON.stringify(prefs),
    }),

  // Jellyfin item lookup (single)
  jellyfinItem: (imdb_id: string) =>
    http<{ jellyfin_id: string | null; jellyfin_url: string | null }>(`/ui/api/jellyfin/item?imdb_id=${encodeURIComponent(imdb_id)}`),

  // Jellyfin batch lookup
  jellyfinItems: (imdb_ids: string[]) =>
    http<{ jellyfin_url: string | null; items: Record<string, string | null> }>(
      `/ui/api/jellyfin/items?imdb_ids=${imdb_ids.map(encodeURIComponent).join(',')}`,
    ),

  // TMDB find by IMDB id
  tmdbFind: (imdb_id: string) =>
    http<{ tmdb_id: number | null; media_type: string | null }>(`/ui/api/tmdb/find?imdb_id=${encodeURIComponent(imdb_id)}`),

  // Library / dashboard
  session: () => http<SessionInfo>('/ui/api/session'),
  stats: () => http<any>('/ui/api/stats'),
  libraryStatusMap: () => http<Record<string, string>>('/ui/api/library/status-map'),
  libraryMovies: () => http<{ items: any[] }>('/ui/api/library/movies'),
  recent: () => http<{ items: any[] }>('/ui/api/activity'),
  myRequests: () => http<{ items: any[] }>('/ui/api/user-requests?mine=1'),

  // Arr import
  arrTest: (kind: 'radarr' | 'sonarr') =>
    http<{ ok: boolean; error?: string }>(`/ui/api/arr-import/test-${kind}`, {
      method: 'POST',
    }),
  arrRun: (kind: 'radarr' | 'sonarr') =>
    http<{ ok: boolean }>(`/ui/api/arr-import/${kind}`, {
      method: 'POST',
      body: JSON.stringify({ only_monitored: true }),
    }),
  arrStatus: () =>
    http<{
      running: boolean;
      kind: string | null;
      total: number;
      done: number;
      added: number;
      skipped: number;
      errors: number;
      message: string;
    }>('/ui/api/arr-import/status'),

  autoAddNow: () =>
    http<{ ok: boolean; message?: string }>('/ui/api/auto-add-now', { method: 'POST' }),

  // Wanted lists
  wantedMovies: () => http<{ items: WantedMovie[] }>('/ui/api/wanted-movies'),
  wantedRecheck: () => http<{ ok: boolean; message?: string }>('/ui/api/wanted-recheck', { method: 'POST' }),
  wantedEpisodes: () => http<{ items: WantedEpisode[] }>('/ui/api/wanted-episodes'),

  // Failed processing requests
  failedRequests: () => http<{ items: any[] }>('/ui/api/requests/failed'),
  retryRequest: (id: number) =>
    http<{ ok: boolean; title?: string }>(`/ui/api/requests/${id}/retry`, { method: 'POST' }),
  deleteRequest: (id: number) =>
    http<{ ok: boolean }>(`/ui/api/requests/${id}/delete`, { method: 'POST' }),

  // Trakt
  traktStatus: () =>
    http<{ connected: boolean; username: string | null; synced_at: string | null; configured: boolean }>(
      '/ui/api/trakt/status'
    ),
  traktAuthStart: () =>
    http<{ user_code: string; verification_url: string; expires_in: number; interval: number }>(
      '/ui/api/trakt/auth/start', { method: 'POST' }
    ),
  traktAuthPoll: () =>
    http<{ status: string; username?: string; error?: string }>('/ui/api/trakt/auth/poll'),
  traktRevoke: () =>
    http<{ ok: boolean }>('/ui/api/trakt/auth/revoke', { method: 'POST' }),
  traktSync: () =>
    http<{ ok: boolean; added: number }>('/ui/api/trakt/sync', { method: 'POST' }),
  traktSyncWatched: () =>
    http<{ ok: boolean; watched: number }>('/ui/api/trakt/sync-watched', { method: 'POST' }),
  traktWatched: () =>
    http<{ imdb_ids: string[] }>('/ui/api/trakt/watched'),
  traktWatchedEpisodes: () =>
    http<{ shows: Record<string, Record<string, number[]>> }>('/ui/api/trakt/watched-episodes'),
  traktScrobble: (params: {
    action: 'start' | 'pause' | 'stop';
    media_type: string;
    imdb_id: string;
    progress: number;
    season?: number;
    episode?: number;
    title?: string;
  }) =>
    http<{ ok: boolean }>('/ui/api/trakt/scrobble', {
      method: 'POST',
      body: JSON.stringify(params),
    }),

  // Maintenance
  repairStrms: () =>
    http<{ scanned: number; ok: number; orphaned_tokens: number; relinked: number; deleted: number; skipped: number }>(
      '/ui/api/repair-strms', { method: 'POST' }
    ),
};

// Image helpers  -  TMDB image CDN
export const tmdbImg = {
  poster: (p: string | null | undefined) => (p ? `https://image.tmdb.org/t/p/w342${p}` : null),
  backdrop: (p: string | null | undefined) => (p ? `https://image.tmdb.org/t/p/w1280${p}` : null),
  logo: (p: string | null | undefined) => (p ? `https://image.tmdb.org/t/p/w92${p}` : null),
  profile: (p: string | null | undefined) => (p ? `https://image.tmdb.org/t/p/w185${p}` : null),
};

// Provider IDs (NL)  -  keep in sync with backend tmdb.NL_PROVIDERS
export const NL_PROVIDER_IDS = {
  netflix: 8,
  amazon_prime: 119,
  disney_plus: 337,
  hbo_max: 1899,
  apple_tv_plus: 350,
  videoland: 563,
  npo_plus: 271,
  skyshowtime: 1773,
} as const;
