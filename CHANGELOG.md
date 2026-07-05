# Changelog

All notable changes to Mycelium are documented in this file.

## [0.6.1] - 2026-07-05

A security- and correctness-focused release from a full multi-pass code review. No new features.

### Security

- OIDC and trusted-proxy logins no longer implicitly become admin - `auth.py` now resolves or creates a real per-user role (`user` by default; only the very first user ever provisioned this way becomes admin, and only during initial, incomplete setup)
- `AUTH_SESSION_SECRET` is no longer used to sign sessions when left at the well-known default value - a random secret is generated and persisted instead, same pattern as the existing `WEBHOOK_SECRET_AUTO`
- Added `is_admin()` checks to roughly 30 previously-unprotected `/ui/*` and `/ui/api/*` routes: settings save/reset, backup restore, DB vacuum/prune, cleanup/repair/migrate triggers, Zilean sync/import, wanted-recheck, NFO/strm regeneration, and several legacy `/api/*` aliases that had slipped through
- `/admin` itself now redirects non-admin users to login instead of only checking that setup is complete
- Web Player `/stream/<token>/*` playback routes now require an authenticated session with the Web Player feature enabled - previously reachable by anyone who obtained a token
- `TRUSTED_PROXY_NETWORKS` default narrowed from broad private-IP ranges to loopback only
- Webhook secret and internal token comparisons now use constant-time comparison throughout
- The Spore TCP server (port 8089, unauthenticated protocol) now binds to loopback by default instead of all interfaces

### Fixed

- A transient scraper/cache-check error on a single episode could mark an entire multi-season request "failed", discarding seasons that had already been added successfully
- The retry queue could silently drop a failed retry and abort the rest of that cycle's batch instead of continuing
- Cleanup/repair and canonical-name migration could leave orphaned database rows behind after deleting or merging `.strm` files, permanently blocking recreation of that title
- Folder rename/merge database updates could corrupt a sibling folder's paths when one folder name was a literal prefix of another (e.g. "Alien (1979)" vs. "Alien (1979) Directors Cut")
- Duplicate-folder merges could silently delete a file that was never actually copied over first
- Plex's fast-start MP4 cache could corrupt sample offsets for CDN files with a second data block after the `moov` atom (dual-mdat layout)
- HTTP suffix byte-ranges (`bytes=-N`) were parsed as the first N bytes instead of the last N
- CSRF protection was effectively disabled on roughly 27 internal API routes because the exemption predated the frontend actually sending the CSRF token
- Several background jobs (series monitor, retry queue) could abort an entire batch when a single item raised an unexpected error instead of continuing with the rest
- Assorted smaller fixes: SQLite `LIKE` wildcard characters in folder names could cause wrong-path matches during renames; a webplayer seek race could start two concurrent FFmpeg processes for the same session; two `/api/*` routes referenced an unimported module and would have raised on use

## [0.6.0] - 2026-07-04

### Credits

Several of the bugfixes in this release were discovered and/or confirmed through the work of [Ventrex](https://github.com/Ventrex/mycelium) in his fork ("VenFlix") and the accompanying [GitHub Discussions](https://github.com/corveck79/mycelium/discussions). Thanks for digging into these issues and sharing the fixes/ideas with the community.

Thanks also to [Damosso](https://github.com/Damosso) for the Seerr webhook secret tip in [#41](https://github.com/corveck79/mycelium/issues/41), which shaped a docs fix earlier in this cycle.

### Added

- **Trakt**: auto-request new watchlist items for download (not just watchlist sync), capped daily, built into the existing Trakt plugin
- **MDBList integration**: connect your own API key, pick lists to sync, capped auto-request
- **Auto-approve**: per-genre rules with year ranges, follow favorite actors (auto-requests their filmography, excludes talk shows/soaps), shared daily budget
- **Discover genre tabs**: admin-configurable browse rows per genre + year range
- **Language filter**: per-user include/exclude of content by original language in Discover
- **Clickable cast**: cast in the detail modal opens an actor page with bio + filmography + Follow button
- **TorBox library scan**: reads existing TorBox cache and creates `.strm` files for anything missing (e.g. after a DB reset)
- **Notification settings** in the React Settings page (Discord/Telegram)
- **Real topbar search bar** instead of just a link to the search page
- **React Admin dashboard finally routed**: `/admin` now shows a tab between the new dashboard (user management, Radarr/Sonarr import, Auto-approve, genre tabs, maintenance) and the existing Jinja page - this page already existed but was never wired to a route

### Fixed

- Settings-UI overrides were silently ignored in several places (Zilean, TMDB, RealDebrid, TorBox, OpenSubtitles, catbox) due to frozen `config.py` imports instead of `settings.get()`
- Mislabeled cams/trailers (e.g. "2160p" that's actually a cam) are now rejected based on physically plausible file size vs. TMDB runtime
- Unreleased titles could pull in fake/cam releases - now blocked via TMDB release date
- Multi-season series only got season 1 into the library
- Duplicate episode tokens/strms when title sanitizing landed differently
- `db.insert_request()` could update the wrong row on retry (SQLite `lastrowid` quirk), leaving requests permanently stuck on "rate_limited"
- TorBox timeouts were treated as success, writing a `.strm` before the torrent was actually ready
- Series could end up split across multiple folders due to varying release names
- Jellyfin library refresh had no debounce, could fire excessively during bulk operations
- Raw IMDb IDs (`tt1234567`) instead of titles shown in notifications/UI for requests without a title in the payload
- Toggle switches in the admin user panel rendered incorrectly (knob always on the right regardless of state)
- Clickable cast was invisible due to a z-index conflict between the detail and actor modals
- Removed a duplicate, colliding Trakt integration (a new build on top of an already-existing plugin) - including a database schema conflict that broke the existing plugin
- Web Player: `/ui/api/web-player/status/<job_id>` silently dropped `token`/`stream_type` from its JSON response, so the frontend always fell into the HLS.js branch (pointed at a raw MP4 redirect instead of a playlist) instead of direct-playing eligible files, causing an infinite retry/timeout loop

## [0.5.2] - 2026-06-12

### Added

- **Web Player VA-API**: hardware-accelerated HEVC transcoding via VA-API (`renderD128`); reduces CPU usage significantly on supported hardware
- **Web Player HEVC-always**: HEVC is always transcoded to HLS regardless of codec; direct serve only for H264 to avoid browser incompatibility
- Docker Compose: `videodriver` GID 937 added for VA-API `renderD128` access
- **Spore wrapper EAE detection**: also detects EAE need from output encoder args (e.g. Shield TV requesting `eac3_eae` output via eARC); skips injecting native decoder hint when output is `copy` to prevent EAE init failures on HTTP input

### Fixed

**Web Player**
- Black screen / corrupt green output on 10-bit HEVC with VA-API (Apollo Lake J3455)
- `scale_vaapi` failure on 10-bit HEVC sources
- Stale segments causing black screen after seek or restart
- Missing `/direct`, `/convert-hls`, `/hls-status` routes
- HLS buffer increased to prevent stalls on slow CDN
- Temp directory leak when HLS conversion crashes before session registration
- `ffmpeg.log` file handle not closed on `Popen` failure
- `shutil.rmtree` called before ffmpeg process exits (race condition)

**Security**
- Session fixation: `session.clear()` now called before writing new session keys on login
- `/torbox-webhook` and `/ui/api/repair-strms` now require authentication
- `/setup/save` now validates against a known-key allowlist (previously accepted arbitrary keys)
- `/health` no longer leaks internal exception details in the response body

**Data integrity**
- `cleanup.py`: new strm written via `process_torrent` before the old one is deleted
- `upgrader.py`: season-pack strms written before per-episode strms are removed
- `mp4_faststart.py`: `.fsh` cache written atomically via temp-file + rename; ftyp box fetched at actual size instead of hardcoded 64 bytes

**Logic**
- `torbox.py`: `metaDL_done` state never matched because `download_state` is lowercased before comparison — fixed to `metadl_done`
- `torbox.py`: createtorrent quota now recorded after HTTP success, not before (prevented quota inflation on network errors)
- `torrentio.py`: season-pack regex `s0?N` → `s0*N(?!\d)` to correctly match zero-padded season codes
- `catbox.py`: `release_idle()` no longer aborts on first network error — each torrent deletion is now wrapped in try/except
- `monitor.py`: aired episodes without a strm are now marked `wanted` in the DB (were silently left without status)
- `retry_queue.py`: startup crash on undefined `_CREATETORRENT_LIMIT` constant (should be `_CREATETORRENT_LIMIT_HOUR`)
- `db.py`: `_migrate()` ALTER TABLE loop now catches per-column errors instead of aborting remaining migrations

**Fresh install**
- Fixed crash `sqlite3.OperationalError: no such table: settings` on first boot when the DB is empty ([#34](https://github.com/corveck79/mycelium/issues/34))

---

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
