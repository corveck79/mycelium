export type MediaType = 'movie' | 'tv';

export interface TmdbItem {
  tmdb_id: number;
  media_type: MediaType;
  title: string;
  original_title?: string;
  year: string | null;
  rating: number;
  votes: number;
  popularity: number;
  overview: string;
  poster_path: string | null;
  backdrop_path: string | null;
  genre_ids?: number[];
}

export interface Provider {
  id: number;
  name: string;
  logo_path: string | null;
  priority?: number;
}

export interface TmdbDetail extends TmdbItem {
  imdb_id?: string;
  runtime?: number;
  genres?: string[];
  tagline?: string;
  status?: string;
  homepage?: string;
  seasons?: Array<{
    season_number: number;
    episode_count: number;
    name: string;
    poster_path: string | null;
    air_date: string | null;
  }>;
  number_of_seasons?: number;
  number_of_episodes?: number;
  cast?: Array<{ name: string; character: string; profile_path: string | null }>;
  trailers?: Array<{ key: string; name: string; site: string }>;
  providers?: { flatrate: Provider[]; link: string | null };
  recommendations?: TmdbItem[];
}

export interface WatchlistItem {
  id: number;
  user_id: number;
  imdb_id: string;
  tmdb_id: number | null;
  media_type: MediaType;
  title: string;
  poster_path: string | null;
  added_at: string;
}

export interface UserRecord {
  id: number;
  username: string;
  role: 'admin' | 'user';
  quota_monthly: number;
  auto_approve: boolean;
  enabled: boolean;
  last_login: string | null;
  created_at: string;
}

export interface UserRequest {
  id: number;
  user_id: number;
  username?: string;
  imdb_id: string;
  tmdb_id: number | null;
  media_type: MediaType;
  title: string;
  status: 'pending' | 'approved' | 'denied';
  reviewed_at: string | null;
  note: string | null;
  created_at: string;
}

export interface SessionInfo {
  authenticated: boolean;
  user?: { id: number; username: string; role: string; auto_approve: boolean } | null;
}

export interface WantedMovie {
  imdb_id: string;
  tmdb_id: number | null;
  title: string;
  reason: string | null;
  attempts: number;
  added_at: string;
  last_checked: string | null;
}

export interface WantedEpisode {
  id: number;
  imdb_id: string;
  tmdb_id: number | null;
  title: string;
  season: number;
  episode: number;
  air_date: string | null;
  status: string;
  attempt_count: number;
  first_attempted: string | null;
  last_attempted: string | null;
}
