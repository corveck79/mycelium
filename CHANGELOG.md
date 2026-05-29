# Changelog

All notable changes to Mycelium are documented in this file.

## [0.5.1-dev] - 2026-05-29

### Added

- **Library poster grid**: movies tab now shows a paginated poster grid (24/page) with the same look as Discover and Watchlist
- **Library search and filters**: search box and All / Available / Wanted filter tabs in the movies view
- **Open in Jellyfin preference**: per-user toggle in Settings > Preferences; clicking a library poster opens the item directly in Jellyfin web instead of the detail modal
- **Jellyfin batch lookup**: Jellyfin item IDs are pre-fetched in one call so poster clicks are synchronous (no popup-blocker issues)
- **Lazy poster loading**: posters missing from the local cache are fetched on first render without blocking the page

### Fixed

- GitHub Actions arm64 build crash: removed dead `spore-builder` Dockerfile stage that compiled a C LD_PRELOAD library using `stat64`/`__xstat64` which do not exist on aarch64
- Jellyfin click mode not working after toggle: Settings now uses an optimistic session-cache update so Library reacts instantly without a page reload
- Detail modal not opening for older items that lack a stored `tmdb_id` (now resolved via `/ui/api/tmdb/find`)

---

## [0.5.0-dev] - 2026-05-28

### Added

- **Mycelium Spore** (experimental Plex integration): stream via stub MKV library + transcoder wrapper, no rclone or local storage required
- **Spore fast-start cache**: moov-first MP4 cache (`.fsh` files) built on first play so subsequent plays are instant
- **Spore track persistence**: audio/subtitle tracks and duration saved to DB after first ffprobe; stubs are regenerated with real tracks on container restart
- **Spore CDN preload**: fast-start cache and ffprobe run automatically when a CDN URL is first resolved, so first play is instant even before user interaction

### Fixed

- TorBox outage no longer causes a 6-hour retry delay for affected items
- HDR10+ no longer treated as a valid HDR10 fallback in the Dolby Vision P5 filter
- Bulk rename for items stored with raw IMDB codes as title (Admin > Maintenance > Fix IMDB titles)
- HEVC compatibility fix in the webplayer plugin for browser playback

---

## [0.4.2] - 2026-05-25

### Added

- `WEBHOOK_SECRET` auto-generation with copy button in admin Settings
- Metrics endpoint secured with optional Bearer token
- Rate limiting on authentication endpoints

### Fixed

- Setup wizard now closes after first run (re-open via Settings)
- WebDAV auth hardening and security headers

---

## [0.4.1] - 2026-05-25

### Added

- Docker Hub CI/CD pipeline on release tags (multi-arch images)
- Splash screen as login background

---

## [0.4.0] - 2026-05-25

### Added

- `LITE_MODE` for webhook-only deployments without heavy background schedulers
- Settings tab in admin dashboard (hot-reload quality filters and runtime config)

### Changed

- Setup wizard UI improved

---

## [0.3.0-beta] - 2026-05-24

### Added

- **Web Player plugin**: in-browser HLS player with subtitle picker
- **Trakt plugin**: watchlist sync and ratings integration
- **Plugin slot system**: plugins can inject components into the frontend (episode player, settings cards)
- Web Player: HDR detection and SDR-only release selection for browser compatibility
- Web Player: multi-audio HLS master playlist with separate audio streams

---

## [0.2.0-beta] - 2026-05-22

### Added

- **Multi-user authentication** with roles (admin/user) and pending approval flow
- **OIDC/SSO support** for single sign-on
- Users tab in admin with pending approval management
- Redesigned React SPA: Library status indicators, region picker

### Fixed

- Open redirect vulnerability on login
- `/setup` accessible without authentication

---

## [0.1.0-beta.1] - 2026-05-22

First public beta. Mycelium has been running in production for several
users; this release formalizes versioning and adds CI/CD.

### Added

- **React SPA** with Discover, Library, Watchlist, Search, Requests, and Wanted pages
- **Setup wizard** walks through TorBox, Jellyfin, TMDB, quality preferences, and Catbox config on first launch
- **Catbox mode** (lazy materialization): torrents added to TorBox on-demand at playback, removed after idle
- **Multi-user auth** with password and OIDC support, role-based access (admin/user)
- **Auto-upgrade**: background job upgrades existing releases when better quality becomes available
- **Season pack consolidation**: replaces individual episode files when a full season pack is found
- **Zilean + Torrentio combined search**: both sources queried and deduplicated for maximum coverage
- **Checkcached batching**: hashes sent in groups of 100 to avoid 414 URI Too Long errors
- **Language filtering**: exclude unwanted audio languages, prefer specific languages
- **Dolby Vision Profile 5 filter**: blocks DV releases without HDR10 fallback layer
- **Separate EXCLUDE_BLURAY option**: BluRay encodes allowed by default, remux filtered separately
- **Blacklist system**: failed info_hashes tracked and excluded from future attempts
- **Playability state tracking**: per-item failure reasons (TB_429, NO_RELEASE, TIMEOUT, etc.)
- **Discord and Telegram notifications** on success/failure
- **OpenSubtitles integration** for automatic subtitle downloads
- **WebDAV server** (optional) for Plex/Emby compatibility
- **RealDebrid support** as fallback debrid provider
- **Radarr/Sonarr bulk import** for migrating existing libraries
- **Community install guide** by Ventrex (EN/NL, Proxmox/NAS)
- **Admin dashboard** with Overview, Requests, Blacklist, Maintenance, Settings, and Logs tabs
- **Pagination** on admin tables (25/50/100/250 rows)
- **CI/CD**: GitHub Actions builds multi-arch Docker images on tag push to GHCR

### Fixed

- Startup crash when duplicate imdb_id rows exist in requests table
- Monitor loop continuing after checkcached 429 (now backs off 60s in catbox mode)
- Upgrader crash from renamed rate limit constant
- Source field showing first word of torrent name instead of torrentio/zilean
- REMUX filter blocking all BluRay encodes (now only blocks actual remux)

### Changed

- Admin page embeds seamlessly in SPA (no double topbar when accessed via sidebar)
- Admin colors matched to SPA palette
- Repair tab renamed to Maintenance with grouped action cards
- Quality preferences and filters are hot-reloadable via Settings (no restart needed)
