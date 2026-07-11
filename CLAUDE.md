# Mycelium

Self-hosted media-request-and-stream pipeline. Watchlist clicks → `.strm` files in Jellyfin via TorBox, zero local storage.

## Kritieke regels

- NOOIT em-dashes (`--`) gebruiken, nergens in code of tekst
- Repo is PUBLIEK op GitHub -- geen wachtwoorden, tokens of IP-adressen committen
- Altijd werken op branch `main` tenzij anders afgesproken
- Docker beheer altijd op de NAS zelf via SSH (zie Commando's)
- GEEN Co-Authored-By in commit messages

## Omgeving

| Component | Waarde |
|---|---|
| NAS | Synology, 10.0.0.10 |
| Mycelium URL | http://10.0.0.10:8088 |
| Projectmap NAS | /volume1/docker/mycelium/ |
| Mount CachyOS | /mnt/nas-docker/mycelium/ |
| Container | `mycelium`, poort 8088 |
| Jellyfin | container `jellyfin`, poort 8096 |
| Plex | container `plex`, poort 32400, docker-compose in `/volume1/docker/plex/` |
| Debrid | TorBox primair; optionele RealDebrid-fallback via `MULTI_DEBRID_ENABLED` (standaard uit) |
| Gebruikers | 4-6 echte gebruikers |
| Repo | corveck79/mycelium (publiek GitHub) |
| Branch | main |

## Stack

- **Backend**: Python 3.12, Flask, APScheduler, SQLite (WAL mode)
- **Frontend**: React 18 + TypeScript + Vite, gebuild naar `static/app/`, geserveerd op `/`
- **Admin UI**: Jinja2 templates op `/admin` (ingebed als iframe in SPA)
- **Data**: `/data/requests.db` (SQLite), `/data/media/` (.strm bestanden)

## Architectuur

```
User (SPA / of Seerr webhook)
  → processor.py  →  zilean / torrentio  →  torbox (cache-check + add)
  → strm_generator.py  →  /data/media/**/*.strm
  → Jellyfin speelt strm af via /stream/<token> → 302 naar /spore-stream/<token> (moov-first proxy, ook voor Jellyfin sinds 2026-07-04)
  → Plex speelt af via stub MKV + transcoder wrapper → /spore-stream/<token>
```

### Catbox flow (kern van het systeem)

`.strm` bestanden bevatten een proxy-URL `/stream/<token>`. Torrent wordt pas aan TorBox toegevoegd bij playback, en na inactiviteit weer verwijderd.

```
GET /stream/<token>
  → catbox.materialize(token)
     1. Check fail cooldown (429 / vorige fout)
     2. Check scan-burst guard (max 3x/minuut)
     3. Check URL cache (actieve TorBox CDN URL?, TTL 23u -- kan korter dan dat al doodgaan, zie liveness-check hieronder)
     4. Check TorBox library (hash aanwezig?)  → directe CDN URL
     5. Zoek gecachede release via Torrentio/Zilean
        → gevonden: voeg toe aan TorBox
        → niet gevonden: 6h cooldown, strm NIET verwijderen
     6. Wacht op TorBox ready (poll max 10 min)
     7. Haal CDN URL op, cache, 302 naar /spore-stream/<token>
        → MKV/non-MP4: HEAD-check op de CDN url voor de finale 302 (dode cache-entry
          → invalidate + herresolve), daarna 302 naar CDN
        → MP4: proxied via mp4_faststart (moov-first cache)
```

### Twee .strm modi

- **Fixed**: directe TorBox CDN URL (vervalt ~24h)
- **Catbox** (`CATBOX_MODE=true`): proxy URL `/stream/<token>`, torrent on-demand

## Mycelium Spore (Plex integratie)

Plex bibliotheek op basis van stub MKV bestanden + transcoder wrapper. LD_PRELOAD is VERWIJDERD.

### Architectuur

```
Plex scant /data/plex-media/**/*.mkv  (stub MKVs)
  → Gebruiker speelt af in Plex
  → Plex kiest "direct stream video + transcode audio" (nooit direct play)
  → Plex Transcoder aangeroepen met -i /plex-media/film.mkv
  → plex_transcoder_wrapper.sh vervangt -i arg door:
       http://127.0.0.1:8088/spore-stream/<token>
  → /spore-stream/<token>:
       Warm (cache klaar): serveert moov-first MP4 (ftyp+moov vooraan)
       Koud (eerste play): pass-through Range proxy naar CDN + bouwt cache in achtergrond
  → FFmpeg leest echte MP4, kopieert video, transcodeert audio
```

### Bestanden Spore

| Bestand | Rol |
|---|---|
| `spore/plex_transcoder_wrapper.sh` | Vervangt Plex Transcoder binary, herschrijft `-i stub.mkv` naar `/spore-stream/TOKEN` |
| `spore_server.py` | TCP server poort 8089 (voor spore_server; momenteel minder relevant) |
| `mp4_faststart.py` | Bouwt moov-first MP4 cache (.fsh bestanden), serveert virtuele byte ranges |
| `spore-smb/` (Rust) | Losstaand SMB2/3-share proces, poort 445 (mapped 4450), read-only anonieme toegang tot de virtuele library. Eigen token-bucket rate limiter + 429-retry richting TorBox CDN. Draait naast gunicorn in dezelfde container (zie Dockerfile CMD) |
| `spore-nfs/` (Go) | NFSv3-tegenhanger van spore-smb, poort 2049 |
| `strm_generator.py` | `make_stub_mkv()`, `_write_spore_stubs()`, `update_stub_from_probe()` |

### Stub MKV opbouw

```
/data/plex-media/movies/Film (2024)/
  Film (2024).mkv    <- stub: EBML header + Tracks (video + audio + subs) + lege Cluster
  Film (2024).minfo  <- token=<hex>\nsize=<bytes>
```

**Tracks in stub:**
- Video: `V_MPEGH/ISO/HEVC` voor 4K, `V_MPEG4/ISO/AVC` voor 1080p/720p
- Audio: `A_TRUEHD` placeholder (forceert transcoder, nooit direct play). Na eerste play: vervangen door echte tracks uit ffprobe (codec, taal, kanalen).
- Subtitels: na eerste play toegevoegd vanuit ffprobe

**Auto-update na eerste play:**
`/spore-stream/<token>` bouwt fast-start cache + ffprobet CDN URL in achtergrond.
`update_stub_from_probe(token, audio, subs)` schrijft stub opnieuw met echte tracks.
Daarna: Plex re-analyzeren (handmatig of via "Fix Incorrect Match") om audio/subtitle-keuze te activeren.

### Fast-start cache (.fsh bestanden)

TorBox CDN MP4 bestanden hebben moov aan het EINDE (mdat-before-moov). FFmpeg moet 15GB seeken.

`mp4_faststart.build_and_cache(cdn_url, token)`:
1. HEAD naar CDN voor bestandsgrootte
2. Fetch ftyp (64 bytes) + laatste 32MB (bevat moov)
3. Rewrite stco/co64 chunk offsets: `+moov_size` (mdat schuift rechts)
4. Cache als `.fsh`: `[8B ftyp_size][8B moov_size][8B cdn_size][ftyp+moov bytes]`

Virtueel layout: `[ftyp][moov_rewritten][mdat via CDN met offset -moov_size]`

### Plex docker-compose

`/volume1/docker/plex/docker-compose.yml` - entrypoint kopieert wrapper script:
```yaml
entrypoint:
  - /bin/sh
  - -c
  - |
    if [ ! -f '/usr/lib/plexmediaserver/Plex Transcoder.real' ]; then
      mv '/usr/lib/plexmediaserver/Plex Transcoder' '/usr/lib/plexmediaserver/Plex Transcoder.real'
    fi
    cp /spore/plex_transcoder_wrapper.sh '/usr/lib/plexmediaserver/Plex Transcoder'
    chmod +x '/usr/lib/plexmediaserver/Plex Transcoder'
    exec /init
volumes:
  - /volume1/docker/mycelium/spore:/spore
  - /volume1/docker/mycelium/data/plex-media:/plex-media:ro
```

### Spore commando's

```bash
# Stubs regenereren (na codec/track wijziging)
docker exec mycelium python3 -c "import strm_generator; print(strm_generator.regenerate_spore_stubs())"

# Plex herstarten (nieuwe wrapper)
ssh corveck@10.0.0.10 "cd /volume1/docker/plex && docker compose up -d"
```

## Bestanden

| Bestand | Rol |
|---|---|
| `app.py` | Flask app, scheduler, alle routes |
| `processor.py` | Pipeline: scrape → TorBox → strm |
| `catbox.py` | Lazy materialization engine |
| `strm_generator.py` | Batch strm herstel/aanmaak, stub MKV generatie |
| `mp4_faststart.py` | MP4 moov-first cache voor Plex spore-stream |
| `spore_server.py` | TCP Range server poort 8089 |
| `cleanup.py` | Opruimen dode/dubbele strm bestanden |
| `monitor.py` | Achtergrondtaken: series sync, Seerr sync |
| `upgrader.py` | Auto-upgrade kwaliteit, season-pack consolidatie |
| `torrentio.py` | Scraper + kwaliteitsfilters |
| `torbox.py` | TorBox API client, rate-limit bewaking |
| `db.py` | Alle SQLite toegang |
| `config.py` | Env vars met defaults (read-only op startup) |
| `settings.py` | Runtime-overridable settings in SQLite |
| `auth.py` / `oidc.py` | Authenticatie (password + OIDC) |

### Database tabellen

- `requests` -- verzoeken van Seerr/SPA (imdb_id is UNIQUE, upsert bij duplicaat)
- `virtual_items` -- catbox tokens, hashes, provider, playability state
- `monitored_series` -- series die Mycelium bijhoudt
- `wanted_episodes` -- per-episode tracking met air_date
- `playability_state` -- reden van falen per item (`TB_429`, `RD_429`, `ADD_FAILED`, `TIMEOUT`, `NO_RELEASE`, `OK`)
- `settings` -- key/value runtime config
- `users` -- multi-user auth
- `blacklist` -- info_hashes die niet werken

## Kwaliteitsfilters (torrentio.py)

Toegepast op Torrentio kandidaten in volgorde:

1. `EXCLUDE_DV_P5=true` -- blokkeert Dolby Vision Profile 5 (geen HDR10 fallback). Regex: `\bhdr10(?!\+)\b` (HDR10+ is GEEN veilige fallback)
2. `EXCLUDE_REMUX=true` -- blokkeert remux/bluray rips
3. `EXCLUDE_CAM=true` -- blokkeert CAM/TS/screener
4. `QUALITY_PREFERENCE=1080p,2160p,720p`
5. `MIN_SEEDERS=3`
6. `PREFER_WEBDL=true`, `PREFER_HEVC=true`

Als alle filters niets opleveren: terugval op minder strenge filtering.

## Commando's

```bash
# Logs live (op NAS)
ssh corveck@10.0.0.10 "docker logs -f mycelium"

# Herstarten na codewijziging
ssh corveck@10.0.0.10 "cd /volume1/docker/mycelium && docker compose restart"

# Rebuild na Dockerfile wijziging
ssh corveck@10.0.0.10 "cd /volume1/docker/mycelium && docker compose up -d --build"

# Re-resolve specifiek item
curl -X POST http://10.0.0.10:8088/ui/api/virtual-items/<token>/re-resolve

# Integrity check
curl http://10.0.0.10:8088/ui/api/integrity

# Playability state overzicht
curl http://10.0.0.10:8088/ui/api/playability-state

# Spore stubs regenereren (direct in container)
docker exec mycelium python3 -c "import strm_generator; print(strm_generator.regenerate_spore_stubs())"
```

## Git

```bash
cd /mnt/nas-docker/mycelium
git pull / git push
```

Kleine codewijzigingen kunnen rechtstreeks via het gemounte filesystem; daarna committen en pushen.

## Frontend

```bash
cd frontend
npm run dev    # Vite dev server
npm run build  # → ../static/app/
```

NB: `npm install` faalt op de NAS SMB mount (symlinks niet ondersteund). Kopieer naar /tmp, installeer daar, bouw, en kopieer output terug:

```bash
cp -r frontend /tmp/mycelium-frontend && cd /tmp/mycelium-frontend
npm install && npm run build
cp -r dist/* /mnt/nas-docker/mycelium/static/app/
```

## Tests

```bash
python -m pytest tests/
```

Tests zijn schaars; focus op integratiecorrectheid.

## Key env vars

| Var | Default | Noot |
|---|---|---|
| `TORBOX_API_KEY` | *(verplicht)* | |
| `CATBOX_MODE` | `false` | Lazy materialization |
| `CATBOX_HOST` | *(verplicht bij catbox)* | Extern bereikbare URL voor proxy strms |
| `JELLYFIN_URL` / `JELLYFIN_API_KEY` | | |
| `TMDB_API_KEY` | | Discover UI + metadata |
| `ZILEAN_URL` | | Optionele lokale scraper |
| `AUTH_SESSION_SECRET` | `mycelium-please-change-me` | Wijzigen in productie |
| `SPORE_ENABLED` | `false` | Plex stub MKV + spore-stream proxy |
| `SPORE_MEDIA_PATH` | `/data/plex-media` | Pad voor stub MKVs en .fsh cache |

## URL-structuur

| Pad | Wat |
|---|---|
| `/` | React SPA (Discover, Library, Watchlist, Search, Requests, Wanted) |
| `/login` | Jinja login pagina (password + OIDC) |
| `/admin` | Jinja admin dashboard (iframe in SPA), tabs: Overview, Blacklist, Repair, Settings, Logs |
| `/ui` | 301 redirect naar `/admin` |
| `/ui/api/*` | Backend API endpoints |
| `/stream/<token>` | Catbox endpoint (Jellyfin + Plex): altijd 302 naar `/spore-stream/<token>` |
| `/spore-stream/<token>` | Moov-first proxy voor alle clients: MP4 proxied met Range support, MKV/other 302 naar (live-gecheckte) CDN url |

## Request statussen

| Status | Betekenis |
|---|---|
| `success` | Heeft werkende .strm bestanden |
| `wanted` | Uitgebracht maar nog geen torrent gevonden |
| `upcoming` | Nog niet uitgebracht (Torrentio geeft 403) |
| `failed` | Andere fout bij processing |

## Webplayer plugin

`plugins/webplayer/` -- eigen HLS speler in de Mycelium SPA.
- Zoekt zelfstandig een SDR web-compatibele release (geen HDR, geen DV, geen AV1)
- FFmpeg op server: H264 input = video copy, HEVC/andere = transcode naar H264 libx264 fast crf22
- Audio: AAC-only in browser, TrueHD/DTS/multichannel → AAC stereo 192k
- HLS segmenten in `/tmp/mycelium-player/<hash>/`
- Multi-audio: master.m3u8 met aparte audio streams

## Bekende open punten

1. **Plex audio/subtitle wisselen**: na eerste play wordt stub bijgewerkt met echte tracks. Plex moet daarna handmatig re-analyzeren ("Fix Incorrect Match" of wacht op automatische scan).
2. **Re-resolve knop in Library tab** (zonder curl)
3. **Playability state tabel in UI**
