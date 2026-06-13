import logging
import re
import struct
import threading
import time
from pathlib import Path

import requests as req_lib

_maintenance_lock = threading.Lock()  # prevents migrate + repair running simultaneously

import db
import jellyfin
import settings
import torbox as torbox_mod
import config as cfg
from config import MEDIA_PATH, TORBOX_BASE_URL, SPORE_MEDIA_PATH

log = logging.getLogger(__name__)

_VIDEO_EXTS = {'.mkv', '.mp4', '.avi', '.m4v', '.mov', '.wmv', '.flv', '.ts', '.m2ts', '.webm'}

_EP_RE = re.compile(r'[Ss](\d{1,2})[Ee](\d{1,2})', re.IGNORECASE)
_YEAR_RE = re.compile(r'(?<!\d)((?:19|20)\d{2})(?!\d)')
# Strip leading site/group prefixes from torrent names before parsing:
#   [DEVIL-TORRENTS PL]  /  rutor.info  /  www.UIndex.org  /  HIDRATORRENTS.ORG  etc.
_SITE_PREFIX_RE = re.compile(
    r'^(?:\[[^\]]*\]\s*|(?:www\.|https?://)\S+\s*|'
    r'(?:rutor|hidratorrents|warmachine|xtorrenty|superseed|byethost\d*|'
    r'uindex|devil-torrents)\s*[\.\-]?\s*(?:info|org|pl|com|net)?\s*[-– - \s]*)+'
    , re.IGNORECASE,
)
# Strip leading Cyrillic block (keeps Latin title when torrent has both)
_CYRILLIC_PREFIX_RE = re.compile(r'^[Ѐ-ӿ\s\(\)\[\]\.,\-– - «»]+')
_JUNK_RE = re.compile(
    r'\s*\b(?:2160[pi]?|4K|UHD|1080[pi]?|720[pi]?|480[pi]?|HDTV|WEB[-.]?DL|WEBRip|WEB\b|'
    r'BluRay|BDRip|DVDRip|REMUX|HEVC|x\.?265|x\.?264|AVC|H\.?26[45]|'
    r'AAC\d*|DTS(?:-HD)?|AC3|EAC3|Atmos|TrueHD|HDR(?:10)?|DV|DoVi|SDR|10[Bb]it|8[Bb]it|'
    r'PROPER|REPACK|EXTENDED|DC|THEATRICAL|UNRATED|IMAX|'
    r'YIFY|YTS|NTb|SPARKS|FGT|RARBG|ettv|eztv)\b.*$',
    re.IGNORECASE,
)
_SAFE_RE = re.compile(r'[/\\:*?"<>|]')
_TRAILER_RE = re.compile(
    r'\b(trailer|sample|extras?|featurette|bonus|deleted[. _-]?scenes?|'
    r'behind[. _-]the[. _-]scenes|making[. _-]of|promo|teaser)\b',
    re.IGNORECASE,
)
_MIN_MOVIE_SIZE = 200 * 1024 * 1024  # 200 MB  -  anything smaller is likely a trailer/sample


# =============================================================================
# SHARED UTILITIES
# Naam-parsing, pad-opbouw, bestandsdetectie.
# Gebruikt door zowel Jellyfin .strm als Plex Spore -- voorzichtig wijzigen.
# =============================================================================

def _is_video(name: str) -> bool:
    return Path(name).suffix.lower() in _VIDEO_EXTS


def _is_trailer(file: dict) -> bool:
    return bool(_TRAILER_RE.search(file.get('name') or ''))


def _pick_main_movie_file(files: list[dict]) -> dict | None:
    """Return the best video file for a movie torrent: largest non-trailer file."""
    videos = [f for f in files if _is_video(f.get('name') or '')]
    if not videos:
        return None
    non_trailer = [f for f in videos if not _is_trailer(f)]
    if not non_trailer:
        return None
    big = [f for f in non_trailer if (f.get('size') or 0) >= _MIN_MOVIE_SIZE]
    return max(big or non_trailer, key=lambda f: f.get('size') or 0)


def _pick_episode_file(files: list[dict], season: int, episode: int) -> dict | None:
    """Find the file in a season pack matching SxxExx. Falls back to None if no match."""
    ep_re = re.compile(rf'[Ss]0?{season}[Ee]0?{episode}\b', re.IGNORECASE)
    videos = [f for f in files if _is_video(f.get('name') or '')]
    matched = [f for f in videos if ep_re.search(f.get('name') or '')]
    if matched:
        return max(matched, key=lambda f: f.get('size') or 0)
    return None


def _clean_torrent_name(name: str) -> str:
    """Strip site prefixes and Cyrillic blocks from a raw torrent name."""
    s = _SITE_PREFIX_RE.sub('', name).strip()
    s = _CYRILLIC_PREFIX_RE.sub('', s).strip()
    # Also strip anything left in leading square brackets
    s = re.sub(r'^\[[^\]]*\]\s*', '', s).strip()
    return s or name  # fall back to original if everything got stripped


def _clean(s: str) -> str:
    return re.sub(r'[._]', ' ', s).strip()


def _strip_junk(s: str) -> str:
    return _JUNK_RE.sub('', s).strip(' .-_ ')


def _safe(s: str) -> str:
    return _SAFE_RE.sub('', s).strip().rstrip('([{ -')


def _parse_info(torrent_name: str, file_name: str) -> dict | None:
    """Extract title/year/season/episode from torrent and file names."""
    torrent_name = _clean_torrent_name(torrent_name)
    file_base = re.sub(r'\.[a-zA-Z0-9]{2,5}$', '', file_name)

    # Episode: try file name first (most reliable for season packs)
    for source in (_clean(file_base), _clean(torrent_name)):
        ep_m = _EP_RE.search(source)
        if ep_m:
            season = int(ep_m.group(1))
            episode = int(ep_m.group(2))
            title = _safe(_strip_junk(source[:ep_m.start()]).strip())
            return {'type': 'episode', 'title': title or 'Unknown', 'season': season, 'episode': episode}

    # Movie: find year
    for source in (_clean(torrent_name), _clean(file_base)):
        yr_m = _YEAR_RE.search(source)
        if yr_m:
            year = int(yr_m.group(1))
            title = _safe(_strip_junk(source[:yr_m.start()]).strip())
            return {'type': 'movie', 'title': title or 'Unknown', 'year': year}

    # Fallback: cleaned torrent name as untitled movie
    title = _safe(_strip_junk(_clean(torrent_name)).strip())
    return {'type': 'movie', 'title': title} if title else None


def _strm_path(info: dict) -> Path:
    media = Path(MEDIA_PATH)
    if info['type'] == 'movie':
        year = info.get('year')
        folder = f"{info['title']} ({year})" if year else info['title']
        return media / 'movies' / folder / f"{folder}.strm"
    title = info['title']
    s, e = info['season'], info['episode']
    return media / 'series' / title / f"Season {s:02d}" / f"{title} S{s:02d}E{e:02d}.strm"


def _get_stream_url(torrent_id: int, file_id: int) -> str | None:
    url = f"{TORBOX_BASE_URL.rstrip('/')}/torrents/requestdl"
    params = {
        "token": settings.get("TORBOX_API_KEY", ""),
        "torrent_id": torrent_id,
        "file_id": file_id,
        "zip_link": "false",
    }
    try:
        resp = req_lib.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json() or {}
        return data.get("data") or None
    except Exception as exc:
        log.warning("requestdl failed torrent=%s file=%s: %s", torrent_id, file_id, exc)
        return None


def _extract_year(name: str) -> int | None:
    m = _YEAR_RE.search(name or "")
    return int(m.group(1)) if m else None


# ── Jellyfin: NFO en mapbeheer ────────────────────────────────────────────────
# NFO sidecars, canonieke mapnamen, IMDB-titelherstel.
# Alles hier raakt uitsluitend Jellyfin; Plex Spore stubs blijven onaangetast.

def _fileinfo_xml(quality: str | None,
                   audio_tracks: list[dict] | None = None,
                   sub_tracks: list[dict] | None = None) -> str:
    """Return a <fileinfo><streamdetails> XML block for Plex/Kodi NFO files.

    Plex reads this to determine codec and make Direct Play / transcode decisions
    without having to probe the actual file. Without this, Video=None and Plex
    refuses to play .strm files (error 4294967283).

    quality: '2160p', '1080p', '720p', or None (defaults to 1080p).
    audio_tracks / sub_tracks: real tracks from CDN probe; falls back to defaults.
    """
    q = (quality or "").lower()
    if "2160" in q or "4k" in q or "uhd" in q:
        v_codec, v_w, v_h = "hevc", 3840, 2160
    elif "720" in q:
        v_codec, v_w, v_h = "h264", 1280, 720
    else:
        v_codec, v_w, v_h = "h264", 1920, 1080

    video_xml = (
        "      <video>\n"
        f"        <codec>{v_codec}</codec>\n"
        f"        <width>{v_w}</width>\n"
        f"        <height>{v_h}</height>\n"
        "      </video>\n"
    )

    audio_xml = ""
    if audio_tracks:
        for at in audio_tracks:
            codec   = (at.get("codec") or at.get("codec_name") or "eac3").lower()
            ch      = int(at.get("channels") or 6)
            lang    = ((at.get("tags") or {}).get("language") or at.get("language") or "und")[:3]
            audio_xml += (
                "      <audio>\n"
                f"        <codec>{codec}</codec>\n"
                f"        <channels>{ch}</channels>\n"
                f"        <language>{lang}</language>\n"
                "      </audio>\n"
            )
    else:
        audio_xml = (
            "      <audio>\n"
            "        <codec>eac3</codec>\n"
            "        <channels>6</channels>\n"
            "        <language>und</language>\n"
            "      </audio>\n"
        )

    sub_xml = ""
    for st in (sub_tracks or []):
        lang = ((st.get("tags") or {}).get("language") or st.get("language") or "und")[:3]
        sub_xml += (
            "      <subtitle>\n"
            f"        <language>{lang}</language>\n"
            "      </subtitle>\n"
        )

    return (
        "  <fileinfo>\n"
        "    <streamdetails>\n"
        f"{video_xml}"
        f"{audio_xml}"
        f"{sub_xml}"
        "    </streamdetails>\n"
        "  </fileinfo>\n"
    )


def _write_nfo(strm_path: Path, imdb_id: str | None, tmdb_id: int | None = None,
               media_type: str = "movie", nfo_path: Path | None = None,
               quality: str | None = None) -> None:
    """Write a Kodi/Jellyfin/Plex NFO sidecar. nfo_path overrides the default
    (strm_path.with_suffix('.nfo')) so callers can write tvshow.nfo anywhere.
    Includes <fileinfo><streamdetails> so Plex knows the codec for Direct Play."""
    if not imdb_id and not tmdb_id:
        return
    nfo_path = nfo_path or strm_path.with_suffix(".nfo")
    if nfo_path.exists():
        return
    m = _YEAR_RE.search(strm_path.parent.name)
    year = int(m.group(1)) if m else None
    title = _YEAR_RE.sub("", strm_path.parent.name).replace("()", "").strip() if m else strm_path.parent.name

    fileinfo = _fileinfo_xml(quality)

    uid_tags = ""
    if imdb_id:
        uid_tags += f'  <uniqueid type="imdb" default="true">{imdb_id}</uniqueid>\n'
    if tmdb_id:
        uid_tags += f'  <uniqueid type="tmdb">{tmdb_id}</uniqueid>\n'

    if media_type == "movie":
        year_tag = f"\n  <year>{year}</year>" if year else ""
        content = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f"<movie>\n  <title>{title}</title>{year_tag}\n{uid_tags}{fileinfo}</movie>\n"
        )
    elif media_type == "episode":
        # Per-episode NFO: Plex uses this to read <fileinfo> codec data for .strm playback
        # Season/episode numbers are encoded in the filename (SxxExx); we just need fileinfo
        content = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f"<episodedetails>\n{uid_tags}{fileinfo}</episodedetails>\n"
        )
    else:
        # tvshow.nfo - no fileinfo needed (Plex reads episode NFOs for codec info)
        content = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f"<tvshow>\n  <title>{title}</title>\n{uid_tags}</tvshow>\n"
        )
    try:
        nfo_path.write_text(content, encoding="utf-8")
        log.info("Wrote NFO: %s", nfo_path)
    except Exception as exc:
        log.warning("Could not write NFO %s: %s", nfo_path, exc)


def update_nfo_streamdetails(strm_path: Path, quality: str | None,
                              audio_tracks: list[dict], sub_tracks: list[dict]) -> bool:
    """Update or add <fileinfo><streamdetails> in an existing NFO with real track info.

    Called after CDN probe so Plex gets accurate codec, language, and channel info.
    Rewrites the <fileinfo> block in-place; preserves all other NFO content.
    Returns True if the NFO was updated.
    """
    nfo_path = strm_path.with_suffix(".nfo")
    if not nfo_path.exists():
        return False
    try:
        text = nfo_path.read_text(encoding="utf-8")
        new_fileinfo = _fileinfo_xml(quality, audio_tracks, sub_tracks)
        # Remove existing <fileinfo>...</fileinfo> block if present
        import re as _re
        text = _re.sub(r'\s*<fileinfo>.*?</fileinfo>\s*', '\n', text,
                       flags=_re.DOTALL)
        # Insert before closing tag (</movie>, </tvshow>, </episodedetails>)
        text = _re.sub(r'(</(?:movie|tvshow|episodedetails)>)',
                       f"{new_fileinfo}\\1", text)
        nfo_path.write_text(text, encoding="utf-8")
        log.debug("NFO streamdetails updated: %s", nfo_path.name)
        return True
    except Exception as exc:
        log.warning("Could not update NFO streamdetails %s: %s", nfo_path, exc)
        return False


def backfill_nfo_streamdetails() -> dict:
    """Add/update <fileinfo> in all existing NFO files that lack codec info.

    For items with probed spore_tracks: uses real audio/sub data.
    For unprobed items: uses quality-based defaults (hevc/h264, EAC3 6ch).
    Safe to run multiple times; skips items without a strm_path.
    """
    items = db.get_all_virtual_items()
    updated = skipped = errors = 0
    for item in items:
        strm_path_str = item.get("strm_path")
        if not strm_path_str:
            skipped += 1
            continue
        strm_path = Path(strm_path_str)
        if not strm_path.exists():
            skipped += 1
            continue
        try:
            quality = item.get("quality")
            tracks = db.load_spore_tracks(item["token"]) or {}
            audio  = tracks.get("audio") or []
            subs   = tracks.get("subs") or []
            if update_nfo_streamdetails(strm_path, quality, audio, subs):
                updated += 1
            else:
                skipped += 1
        except Exception as exc:
            log.warning("backfill_nfo: error for %s: %s", strm_path_str, exc)
            errors += 1
    log.info("backfill_nfo_streamdetails: updated=%d skipped=%d errors=%d",
             updated, skipped, errors)
    return {"updated": updated, "skipped": skipped, "errors": errors}


def _canonical_movie_folder(imdb_id: str, fallback_title: str | None = None,
                             fallback_year: int | None = None) -> str:
    """Return the canonical 'Title (Year)' folder name from TMDB for this imdb_id.
    Falls back to fallback_title/year if TMDB lookup fails."""
    try:
        import tmdb as _tmdb
        results = _tmdb._get(f"/find/{imdb_id}",
                             params={"external_source": "imdb_id"}) or {}
        hits = results.get("movie_results") or []
        if hits:
            title = hits[0].get("title") or ""
            year = (hits[0].get("release_date") or "")[:4]
            if title:
                safe = _safe(title)
                return f"{safe} ({year})" if year else safe
    except Exception as exc:
        log.debug("_canonical_movie_folder TMDB lookup failed for %s: %s", imdb_id, exc)
    if fallback_title:
        safe = _safe(fallback_title)
        return f"{safe} ({fallback_year})" if fallback_year else safe
    return ""


def fix_imdb_titles() -> dict:
    """Find requests whose title is still a raw IMDB code (e.g. tt0096697), fetch the
    real title from TMDB, rename the folder on disk, and update the DB + strm paths."""
    import re as _re
    import tmdb as _tmdb

    _IMDB_PAT = _re.compile(r'^tt\d{6,10}$')
    fixed: list[dict] = []
    failed: list[dict] = []

    with db._connect() as conn:
        rows = conn.execute(
            "SELECT imdb_id, title, media_type FROM requests WHERE title LIKE 'tt%'"
        ).fetchall()
    candidates = [dict(r) for r in rows if _IMDB_PAT.match(r['title'] or '')]

    for row in candidates:
        imdb_id   = row['imdb_id']
        old_title = row['title']
        mtype     = row['media_type']   # 'movie' or 'series'
        kind      = 'movie' if mtype == 'movie' else 'tv'

        try:
            data    = _tmdb._get(f"/find/{imdb_id}", params={"external_source": "imdb_id"}) or {}
            key     = 'movie_results' if kind == 'movie' else 'tv_results'
            results = data.get(key) or []
            if not results:
                failed.append({'imdb_id': imdb_id, 'reason': 'TMDB: no results'})
                continue

            hit       = results[0]
            new_title = hit.get('title') if kind == 'movie' else hit.get('name')
            year      = (hit.get('release_date') or hit.get('first_air_date') or '')[:4]
            if not new_title:
                failed.append({'imdb_id': imdb_id, 'reason': 'TMDB: empty title'})
                continue

            new_safe   = _safe(new_title)
            new_folder = f"{new_safe} ({year})" if year else new_safe
            subdir     = 'movies' if mtype == 'movie' else 'series'
            media_root = Path(MEDIA_PATH)

            # Derive current folder from the first matching strm_path in virtual_items
            with db._connect() as conn:
                vi = conn.execute(
                    "SELECT strm_path FROM virtual_items WHERE imdb_id=? AND strm_path IS NOT NULL LIMIT 1",
                    (imdb_id,)
                ).fetchone()
            old_folder_path = None
            if vi and vi['strm_path']:
                parts = Path(vi['strm_path'].replace('\\', '/')).parts
                # strm_path looks like /data/media/series/tt0096697/Season 1/...
                # find the index of subdir
                for idx, p in enumerate(parts):
                    if p == subdir and idx + 1 < len(parts):
                        candidate = media_root / subdir / parts[idx + 1]
                        if candidate.exists():
                            old_folder_path = candidate
                        break

            new_folder_path = media_root / subdir / new_folder

            # Update DB
            with db._connect() as conn:
                conn.execute("UPDATE requests SET title=? WHERE imdb_id=?", (new_title, imdb_id))
                conn.execute("UPDATE virtual_items SET title=? WHERE imdb_id=?", (new_title, imdb_id))
                conn.commit()

            # Rename folder + update strm paths
            renamed = False
            if old_folder_path and old_folder_path != new_folder_path:
                if new_folder_path.exists():
                    # Merge: move contents into existing folder
                    for child in old_folder_path.rglob('*'):
                        if child.is_file():
                            dest = new_folder_path / child.relative_to(old_folder_path)
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            child.rename(dest)
                    old_folder_path.rmdir() if not any(old_folder_path.iterdir()) else None
                else:
                    old_folder_path.rename(new_folder_path)
                db.update_virtual_strm_path_prefix(str(old_folder_path), str(new_folder_path))
                renamed = True

            fixed.append({'imdb_id': imdb_id, 'old': old_title, 'new': new_title,
                          'year': year, 'renamed': renamed})
            log.info("fix_imdb_titles: %s -> %s (renamed=%s)", old_title, new_title, renamed)

        except Exception as exc:
            log.warning("fix_imdb_titles: failed for %s: %s", imdb_id, exc)
            failed.append({'imdb_id': imdb_id, 'reason': str(exc)})

    return {'fixed': fixed, 'failed': failed,
            'total': len(candidates), 'fixed_count': len(fixed)}


def _find_movie_folder_by_imdb(imdb_id: str) -> Path | None:
    """Return an existing movie folder for this imdb_id via virtual_items DB, or None."""
    items = db.get_virtual_items_by_imdb(imdb_id, media_type="movie")
    for item in items:
        strm_path = item.get("strm_path")
        if strm_path:
            p = Path(strm_path).parent
            if p.exists():
                return p
    return None


_preload_semaphore = threading.Semaphore(3)   # max 3 concurrent preload threads
_preload_in_flight: set[str] = set()
_preload_lock = threading.Lock()
_preload_add_lock = threading.Lock()          # gate around add_magnet to respect rate limit
_preload_state = {"last_add": 0.0}            # mutable so no global keyword needed
_PRELOAD_MIN_INTERVAL = 7.0                   # seconds between add_magnet calls (~8/min, limit is 10/min)


# ── Catbox: CDN URL cache ─────────────────────────────────────────────────────
# TorBox CDN URL ophalen en opslaan. Gedeeld door Jellyfin en Spore.

def _cache_cdn_url(info_hash: str, ready_item: dict, title: str) -> None:
    """Fetch CDN URLs for ALL tokens with this hash and update catbox URL cache + Spore stubs.

    Movies: one token, picks largest video file.
    Season packs: N episode tokens sharing the same hash, each gets its own file via SxxExx match.
    """
    try:
        import catbox as _catbox
        torrent_id = ready_item.get("id")
        files = ready_item.get("files") or []
        if not torrent_id:
            return
        if not files:
            # TorBox sometimes omits the files list; force a fresh lookup
            fresh = torbox_mod.find_by_hash(info_hash, force_refresh=True)
            if fresh:
                files = fresh.get("files") or []
                torrent_id = fresh.get("id") or torrent_id
        if not files or not torrent_id:
            log.debug("Preload: no files for %s, CDN cache skipped", title)
            return

        all_items = db.get_virtual_items_by_hash(info_hash)
        if not all_items:
            return

        cached_count = 0
        for vi in all_items:
            token   = vi.get("token")
            season  = vi.get("season")
            episode = vi.get("episode")
            if not token:
                continue
            if season and episode:
                # Episode in a season pack: find the specific file by SxxExx
                f = _pick_episode_file(files, season, episode)
                if not f:
                    log.debug("Preload: no file match S%02dE%02d for %s", season, episode, title)
                    continue
                file_id = f.get("id")
            else:
                # Movie (or single-file torrent): pick the main video file
                main = _pick_main_movie_file(files)
                file_id = (main or files[0]).get("id")

            cdn_url = _get_stream_url(torrent_id, file_id)
            if not cdn_url:
                continue
            _catbox.cache_url(token, cdn_url)
            _preload_spore(cdn_url, token)
            cached_count += 1

        log.info("Preload: %s CDN URLs cached (%d/%d tokens)", title, cached_count, len(all_items))
    except Exception as exc:
        log.debug("Preload: CDN cache skipped for %s: %s", title, exc)


_TRUEHD_CODECS   = frozenset({"truehd", "mlp"})
_SAFE_AUDIO_CODECS = frozenset({
    "eac3", "ac3", "aac", "dts", "flac", "mp3", "opus", "vorbis",
    "pcm_s16le", "pcm_s24le", "pcm_s32le",
})


# ── Plex Spore: audio-voorkeur helpers ────────────────────────────────────────
# Selectie van veilig decodeerbaar audiotrack en .minfo sidecar beheer.
# Raakt ALLEEN Plex .minfo bestanden; geen Jellyfin .strm bestanden.

def _preferred_audio_index(audio_streams: list[dict]) -> int:
    """Return 0-based audio stream index to prefer for FFmpeg -map 0:a:N.

    If the first audio track is TrueHD/MLP and a decode-safe fallback exists
    (EAC3, AC3, AAC, ...), return the fallback's index. Otherwise return 0.
    TrueHD decode often fails mid-stream on CDN files due to missing major-sync
    frames after seeks, causing HLS transcoding (Android/Shield) to stall.
    """
    if not audio_streams:
        return 0
    first_codec = (audio_streams[0].get("codec_name") or "").lower()
    if first_codec not in _TRUEHD_CODECS:
        return 0
    for i, s in enumerate(audio_streams[1:], 1):
        if (s.get("codec_name") or "").lower() in _SAFE_AUDIO_CODECS:
            return i
    return 0


def update_minfo_preferred_audio(token: str, audio_index: int) -> None:
    """Add or update preferred_audio=N in the .minfo sidecar for token.

    The Plex transcoder wrapper reads this to remap '-map 0:a:0' to the
    specified stream index, skipping a corrupt primary TrueHD track.
    """
    item = db.get_virtual_item(token)
    if not item or not item.get("strm_path"):
        return
    strm_path  = Path(item["strm_path"])
    minfo_path = _spore_stub_dir(strm_path) / (strm_path.stem + ".minfo")
    if not minfo_path.exists():
        return
    try:
        lines = minfo_path.read_text(encoding="utf-8").splitlines()
        lines = [l for l in lines if not l.startswith("preferred_audio=")]
        if audio_index > 0:
            lines.append(f"preferred_audio={audio_index}")
        minfo_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log.info("Spore: preferred_audio=%d saved to .minfo for token=%s", audio_index, token)
    except Exception as exc:
        log.warning("Spore: could not update .minfo for token=%s: %s", token, exc)


def _preload_spore(cdn_url: str, token: str, build_fsh: bool = True) -> None:
    """Probe CDN tracks and optionally build fast-start cache for a Plex stub.

    build_fsh=True  -- full preload path: build .fsh cache + ffprobe (used on first play / preload)
    build_fsh=False -- lightweight probe only: ffprobe without downloading 32MB (used for bulk backfill)
    """
    if not settings.get("SPORE_ENABLED", cfg.SPORE_ENABLED):
        return
    try:
        import json as _json, subprocess as _sp
        # Skip probe if already done (preferred_audio detection included)
        existing = db.load_spore_tracks(token)
        if existing and "preferred_audio_idx" in existing:
            return

        v_extra_hex = ""
        if build_fsh:
            import mp4_faststart
            ok = mp4_faststart.build_and_cache(cdn_url, token)
            if not ok:
                return
            # Extract CodecPrivate from the cached .fsh moov (no -show_data needed)
            cp = mp4_faststart.extract_codec_private(token)
            v_extra_hex = cp.hex() if cp else ""

        res = _sp.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", cdn_url],
            capture_output=True, timeout=60,
        )
        if res.returncode != 0:
            return
        data    = _json.loads(res.stdout)
        streams = data.get("streams", [])
        audio   = [s for s in streams if s.get("codec_type") == "audio"]
        subs    = [s for s in streams if s.get("codec_type") == "subtitle"]
        dur     = float(data.get("format", {}).get("duration", 0) or 0)
        preferred_idx = _preferred_audio_index(audio)
        db.save_spore_tracks(token, {
            "audio": audio, "subs": subs, "duration_s": dur,
            "video_extradata_hex": v_extra_hex,
            "preferred_audio_idx": preferred_idx,
        })
        update_stub_from_probe(token, audio, subs, duration_s=dur or None)
        if preferred_idx > 0:
            update_minfo_preferred_audio(token, preferred_idx)
            log.info("Preload: preferred_audio=%d for token=%s (TrueHD -> fallback)",
                     preferred_idx, token)
        log.info("Preload: spore probe done token=%s dur=%.0fs subs=%d fsh=%s",
                 token, dur, len(subs), build_fsh)
    except Exception as exc:
        log.debug("Preload: spore probe failed token=%s: %s", token, exc)


def _preload_torrent(info_hash: str, magnet: str, title: str) -> None:
    """Add torrent to TorBox in the background, wait until ready, then
    fetch and cache the CDN URL so first play is instant.

    Deduplication: only one thread per info_hash runs at a time.
    Concurrency cap: at most 3 preloads run simultaneously (_preload_semaphore).
    Rate gate: at least _PRELOAD_MIN_INTERVAL seconds between consecutive
    add_magnet calls so we stay well under TorBox's 10/min limit. This is
    critical when adding a series without season packs -- 20 episodes spawn
    20 threads but they drip-feed add_magnet one at a time."""
    with _preload_lock:
        if info_hash in _preload_in_flight:
            log.debug("Preload: %s already in flight, skipping", title)
            return
        _preload_in_flight.add(info_hash)
    with _preload_semaphore:
        try:
            existing = torbox_mod.find_by_hash(info_hash)
            if existing and torbox_mod._is_ready(existing):
                log.debug("Preload: %s already ready in TorBox", title)
                ready = existing
            else:
                if not existing:
                    # Rate gate: enforce minimum interval between add_magnet calls
                    with _preload_add_lock:
                        elapsed = time.time() - _preload_state["last_add"]
                        if elapsed < _PRELOAD_MIN_INTERVAL:
                            time.sleep(_PRELOAD_MIN_INTERVAL - elapsed)
                        _preload_state["last_add"] = time.time()
                    torbox_mod.add_magnet(magnet, reason="preload")
                ready = torbox_mod.wait_until_ready(info_hash, timeout=600)
            if not ready:
                log.debug("Preload: %s not ready within timeout", title)
                return
            _cache_cdn_url(info_hash, ready, title)
        except Exception as exc:
            log.debug("Preload: skipped %s: %s", title, exc)
        finally:
            with _preload_lock:
                _preload_in_flight.discard(info_hash)


# =============================================================================
# CATBOX / LAZY STRM  --  RAAKT BEIDE SYSTEMEN
# create_lazy_*_strm schrijft zowel Jellyfin .strm als Plex .minfo + stub MKV.
# Wijzigingen hier kunnen ZOWEL Jellyfin als Plex breken. Na aanpassing:
#   - test Jellyfin: controleer of .strm aangemaakt wordt in MEDIA_PATH
#   - test Plex: controleer of .mkv + .minfo aangemaakt worden in SPORE_MEDIA_PATH
# =============================================================================

def create_lazy_movie_strm(info_hash: str, magnet: str, title: str,
                            year: int | None, imdb_id: str | None = None,
                            tmdb_id: int | None = None, quality: str | None = None,
                            source: str | None = None, size_gb: float | None = None) -> bool:
    """Write a Catbox virtual movie .strm WITHOUT adding the torrent to TorBox.
    createtorrent is deferred until first playback (see catbox.materialize).
    Atomically writes .nfo, poster.jpg, fanart.jpg, and requests subtitles.
    Returns True if a new .strm was written."""
    import catbox

    # imdb_id is leading: check if a virtual_item already exists for this movie
    # (regardless of whether the .strm is present on disk right now).
    if imdb_id:
        existing_items = db.get_virtual_items_by_imdb(imdb_id, media_type="movie")
        if existing_items:
            log.info("create_lazy_movie_strm: virtual_item for %s already exists  -  skipping",
                     imdb_id)
            return False
        existing = _find_movie_folder_by_imdb(imdb_id)
        if existing:
            log.info("create_lazy_movie_strm: folder for %s already exists (%s)  -  skipping",
                     imdb_id, existing.name)
            return False
        folder = _canonical_movie_folder(imdb_id, fallback_title=title, fallback_year=year)
    else:
        folder = _safe(f"{title} ({year})") if year else _safe(title)

    if not folder:
        return False
    path = Path(MEDIA_PATH) / "movies" / folder / f"{folder}.strm"
    if path.exists():
        return False
    token = catbox.register(
        info_hash=(info_hash or "").lower(),
        magnet=magnet,
        title=folder,
        media_type="movie",
        torbox_id=None,
        file_id=None,
        strm_path=str(path),
        imdb_id=imdb_id,
        quality=quality,
        source=source,
        size_gb=size_gb,
        year=year,
    )
    written = _write_strm(path, catbox.proxy_url(token))
    if written:
        _write_spore_stubs(path, token, folder, quality, size_gb)
        if imdb_id or tmdb_id:
            _write_nfo(path, imdb_id, tmdb_id, quality=quality)
        if imdb_id:
            try:
                import nfo_generator
                nfo_generator.fetch_images_for_folder(path.parent, imdb_id, "movie")
            except Exception as exc:
                log.debug("Image fetch skipped for %s: %s", folder, exc)
            try:
                import subtitles
                subtitles.fetch_for(path, imdb_id, "movie")
            except Exception as exc:
                log.debug("Subtitle fetch skipped for %s: %s", folder, exc)
        if settings.get("CATBOX_PRELOAD", cfg.CATBOX_PRELOAD) and info_hash and magnet:
            threading.Thread(
                target=_preload_torrent,
                args=(info_hash, magnet, folder),
                daemon=True,
            ).start()
    return written


def create_lazy_episode_strm(info_hash: str, magnet: str, title: str,
                               season: int, episode: int,
                               imdb_id: str | None = None,
                               quality: str | None = None,
                               source: str | None = None,
                               size_gb: float | None = None,
                               preload_first: bool = False) -> bool:
    """Write a Catbox virtual episode .strm WITHOUT adding to TorBox.
    For season packs: multiple episodes share the same info_hash/magnet;
    catbox.materialize picks the right file by SxxExx at playback time.
    Atomically writes tvshow.nfo and series poster/fanart on first episode.
    Returns True if a new .strm was written."""
    import catbox
    safe_title = _safe(title)
    if not safe_title:
        return False
    season_dir = f"Season {season:02d}"
    ep_name = f"{safe_title} S{season:02d}E{episode:02d}"
    path = Path(MEDIA_PATH) / "series" / safe_title / season_dir / f"{ep_name}.strm"
    if path.exists():
        return False
    token = catbox.register(
        info_hash=(info_hash or "").lower(),
        magnet=magnet,
        title=ep_name,
        media_type="series",
        torbox_id=None,
        file_id=None,
        strm_path=str(path),
        imdb_id=imdb_id,
        quality=quality,
        source=source,
        size_gb=size_gb,
        season=season,
        episode=episode,
    )
    written = _write_strm(path, catbox.proxy_url(token))
    if written:
        _write_spore_stubs(path, token, ep_name, quality, size_gb)
        if imdb_id:
            series_root = path.parent.parent
            tvshow_nfo = series_root / "tvshow.nfo"
            if not tvshow_nfo.exists():
                _write_nfo(path, imdb_id, nfo_path=tvshow_nfo, media_type="series")
            # Per-episode NFO with codec info so Plex can make playback decisions
            _write_nfo(path, imdb_id, media_type="episode", quality=quality)
            try:
                import nfo_generator
                nfo_generator.fetch_images_for_folder(series_root, imdb_id, "tv")
            except Exception as exc:
                log.debug("Image fetch skipped for %s: %s", safe_title, exc)
        if settings.get("CATBOX_PRELOAD", cfg.CATBOX_PRELOAD) and info_hash and magnet:
            threading.Thread(
                target=_preload_torrent,
                args=(info_hash, magnet, ep_name),
                daemon=True,
            ).start()
    return written


def _norm_title(s: str) -> str:
    """Normalize a folder/title string for fuzzy duplicate detection.
    Strips year in parens, leading articles, punctuation, and lowercases."""
    s = re.sub(r'\(\d{4}\)', '', s)           # remove (year)
    s = re.sub(r'^(the|a|an)\s+', '', s, flags=re.IGNORECASE)  # strip leading article
    return re.sub(r'[^a-z0-9]', '', s.lower())  # alphanumeric only


# =============================================================================
# PLEX SPORE: stub MKV generatie
# Alleen Plex-gerelateerde code. Jellyfin .strm bestanden worden hier NIET
# aangeraakt. EBML bytes, .mkv stubs, .minfo sidecars, stub-update na probe.
# =============================================================================

def _ebml_vint(n: int) -> bytes:
    """Encode n as EBML variable-length integer (used for element sizes)."""
    if n < 0x7F:
        return bytes([0x80 | n])
    if n < 0x3FFF:
        return bytes([0x40 | (n >> 8), n & 0xFF])
    if n < 0x1FFFFF:
        return bytes([0x20 | (n >> 16), (n >> 8) & 0xFF, n & 0xFF])
    return bytes([0x10 | (n >> 24), (n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF])


def _ebml_uint(n: int) -> bytes:
    """Encode unsigned integer in minimum bytes."""
    if n == 0:
        return b'\x00'
    result = []
    while n > 0:
        result.append(n & 0xFF)
        n >>= 8
    return bytes(reversed(result))


def _ebml_el(id_bytes: bytes, data: bytes) -> bytes:
    """Build an EBML element: raw ID bytes + VINT(size) + data."""
    return id_bytes + _ebml_vint(len(data)) + data


def _codec_id_for_quality(quality: str | None) -> str:
    """Return MKV CodecID string based on quality hint.
    4K content is virtually always HEVC; 1080p/720p defaults to H.264."""
    if quality:
        q = quality.lower()
        if '2160' in q or '4k' in q or 'uhd' in q:
            return 'V_MPEGH/ISO/HEVC'
    return 'V_MPEG4/ISO/AVC'


_FFCODEC_TO_MKV_AUDIO: dict[str, str] = {
    "eac3":     "A_EAC3",
    "ac3":      "A_AC3",
    "truehd":   "A_TRUEHD",
    "mlp":      "A_TRUEHD",
    "dts":      "A_DTS",
    "aac":      "A_AAC",
    "opus":     "A_OPUS",
    "flac":     "A_FLAC",
    "mp3":      "A_MPEG/L3",
    "vorbis":   "A_VORBIS",
    "pcm_s16le": "A_PCM/INT/LIT",
    "pcm_s24le": "A_PCM/INT/LIT",
    "pcm_s32le": "A_PCM/INT/LIT",
}

_FFCODEC_TO_MKV_SUB: dict[str, str] = {
    "subrip":              "S_TEXT/UTF8",
    "ass":                 "S_TEXT/ASS",
    "ssa":                 "S_TEXT/ASS",
    "webvtt":              "S_TEXT/WEBVTT",
    "hdmv_pgs_subtitle":  "S_HDMV/PGS",
    "dvd_subtitle":        "S_VOBSUB",
    "mov_text":            "S_TEXT/UTF8",
}


def _ebml_audio_track_entry(track_num: int, codec_mkv: str, lang: str,
                             channels: int, sample_rate: float,
                             is_default: bool) -> bytes:
    lang_bytes = lang.encode("ascii", errors="replace")[:3].ljust(3)[:3]
    audio_el = (
        _ebml_el(b'\xB5', struct.pack('>f', sample_rate)) +
        _ebml_el(b'\x9F', _ebml_uint(channels))
    )
    body = (
        _ebml_el(b'\xD7', _ebml_uint(track_num)) +
        _ebml_el(b'\x73\xC5', _ebml_uint(track_num)) +
        _ebml_el(b'\x83', _ebml_uint(2)) +
        _ebml_el(b'\xB9', _ebml_uint(1)) +
        _ebml_el(b'\x88', _ebml_uint(1 if is_default else 0)) +
        _ebml_el(b'\x22\xB5\x9C', lang_bytes) +
        _ebml_el(b'\x86', codec_mkv.encode()) +
        _ebml_el(b'\xE1', audio_el)
    )
    return _ebml_el(b'\xAE', body)


def _ebml_subtitle_track_entry(track_num: int, codec_mkv: str, lang: str) -> bytes:
    lang_bytes = lang.encode("ascii", errors="replace")[:3].ljust(3)[:3]
    body = (
        _ebml_el(b'\xD7', _ebml_uint(track_num)) +
        _ebml_el(b'\x73\xC5', _ebml_uint(track_num)) +
        _ebml_el(b'\x83', _ebml_uint(0x11)) +
        _ebml_el(b'\xB9', _ebml_uint(1)) +
        _ebml_el(b'\x88', _ebml_uint(0)) +
        _ebml_el(b'\x22\xB5\x9C', lang_bytes) +
        _ebml_el(b'\x86', codec_mkv.encode())
    )
    return _ebml_el(b'\xAE', body)


def make_stub_mkv(title: str, quality: str | None = None,
                   duration_sec: float = 7200.0,
                   codec_id: str | None = None,
                   audio_tracks: list[dict] | None = None,
                   subtitle_tracks: list[dict] | None = None,
                   video_codec_private: bytes | None = None) -> bytes:
    """Generate a minimal valid MKV file for Plex scanning.

    audio_tracks: list of dicts with keys codec, language, channels, sample_rate.
    subtitle_tracks: list of dicts with keys codec, language.
    When audio_tracks is None, a PCM 16ch placeholder is used so Plex
    always invokes the transcoder (never direct-plays the stub).
    """
    width, height = 1920, 1080
    if quality:
        q = quality.lower()
        if '2160' in q or '4k' in q or 'uhd' in q:
            width, height = 3840, 2160
        elif '720' in q:
            width, height = 1280, 720
        elif '480' in q:
            width, height = 854, 480

    if codec_id is None:
        # Use quality-based codec so Direct Stream clients (Linux HTPC etc.) get
        # the matching codec from the CDN, preventing Plex from killing the session
        # for a video codec mismatch. VP8 caused such a mismatch: Plex negotiated
        # VP8 Direct Stream with the Linux client, but the CDN has HEVC/H264, so
        # Plex killed immediately.
        codec_id = _codec_id_for_quality(quality)

    # EBML header
    ebml_data = (
        _ebml_el(b'\x42\x86', _ebml_uint(1)) +       # EBMLVersion
        _ebml_el(b'\x42\xF7', _ebml_uint(1)) +       # EBMLReadVersion
        _ebml_el(b'\x42\xF2', _ebml_uint(4)) +       # EBMLMaxIDLength
        _ebml_el(b'\x42\xF3', _ebml_uint(8)) +       # EBMLMaxSizeLength
        _ebml_el(b'\x42\x82', b'matroska') +          # DocType
        _ebml_el(b'\x42\x87', _ebml_uint(4)) +       # DocTypeVersion
        _ebml_el(b'\x42\x85', _ebml_uint(2))          # DocTypeReadVersion
    )
    ebml_header = _ebml_el(b'\x1A\x45\xDF\xA3', ebml_data)

    # Segment Info
    info_data = (
        _ebml_el(b'\x2A\xD7\xB1', _ebml_uint(1_000_000)) +
        _ebml_el(b'\x44\x89', struct.pack('>d', duration_sec * 1000.0)) +
        _ebml_el(b'\x7B\xA9', title.encode('utf-8')) +
        _ebml_el(b'\x4D\x80', b'Mycelium Spore') +
        _ebml_el(b'\x57\x41', b'Mycelium Spore')
    )
    info_el = _ebml_el(b'\x15\x49\xA9\x66', info_data)

    # Video track
    # For 4K HEVC stubs with real codec private, declare HDR10 Colour metadata so
    # Plex can apply tone-mapping for SDR clients. Skipped for VP8 placeholder
    # stubs (HDR10 in a VP8 track is meaningless; the wrapper forces video copy so
    # the actual CDN HDR10 signal passes through untouched regardless).
    is_4k = (width >= 3840) and codec_id.startswith("V_MPEGH/ISO/HEVC")  # HDR10 only for HEVC 4K
    if is_4k:
        # MKV Colour element with BT.2020 + SMPTE ST 2084 PQ (HDR10)
        colour_data = (
            _ebml_el(b'\x55\xB1', _ebml_uint(9))  +   # MatrixCoefficients=9 BT.2020
            _ebml_el(b'\x55\xB2', _ebml_uint(10)) +   # BitsPerChannel=10
            _ebml_el(b'\x55\xB9', _ebml_uint(1))  +   # Range=1 broadcast
            _ebml_el(b'\x55\xBA', _ebml_uint(16)) +   # TransferCharacteristics=16 PQ
            _ebml_el(b'\x55\xBB', _ebml_uint(9))      # Primaries=9 BT.2020
        )
        colour_el = _ebml_el(b'\x55\xB0', colour_data)
    else:
        colour_el = b''

    video_data = (
        _ebml_el(b'\xB0', _ebml_uint(width)) +
        _ebml_el(b'\xBA', _ebml_uint(height)) +
        _ebml_el(b'\x54\xB0', _ebml_uint(width)) +
        _ebml_el(b'\x54\xBA', _ebml_uint(height)) +
        colour_el
    )
    video_track = _ebml_el(b'\xAE', (
        _ebml_el(b'\xD7', _ebml_uint(1)) +
        _ebml_el(b'\x73\xC5', _ebml_uint(1)) +
        _ebml_el(b'\x83', _ebml_uint(1)) +
        _ebml_el(b'\xB9', _ebml_uint(1)) +
        _ebml_el(b'\x88', _ebml_uint(1)) +
        _ebml_el(b'\x86', codec_id.encode()) +
        (_ebml_el(b'\x63\xA2', video_codec_private) if video_codec_private else b'') +
        _ebml_el(b'\xE0', video_data)
    ))

    tracks_data = video_track
    next_num = 2

    if audio_tracks:
        for i, at in enumerate(audio_tracks):
            mkv_codec = _FFCODEC_TO_MKV_AUDIO.get(
                (at.get("codec") or "").lower(), "A_EAC3"
            )
            tracks_data += _ebml_audio_track_entry(
                track_num=next_num,
                codec_mkv=mkv_codec,
                lang=(at.get("language") or "und")[:3],
                channels=int(at.get("channels") or 2),
                sample_rate=float(at.get("sample_rate") or 48000),
                is_default=(i == 0),
            )
            next_num += 1
    else:
        # EAC3 5.1 placeholder.
        #   - A_EAC3 6ch: Plex chooses Direct Stream audio (copy output) for
        #     clients that support EAC3 passthrough (Shield TV + AV receiver via
        #     eARC). No EAE needed. Audio packets copied from CDN.
        #   - For clients that transcode (MiTV -> AC3), EAE decodes EAC3 via
        #     eac3_eae IPC. The wrapper keeps -eae_prefix for transcode sessions.
        tracks_data += _ebml_audio_track_entry(
            track_num=2, codec_mkv="A_EAC3", lang="und",
            channels=6, sample_rate=48000.0, is_default=True,
        )
        next_num = 3

    for st in (subtitle_tracks or []):
        mkv_codec = _FFCODEC_TO_MKV_SUB.get(
            (st.get("codec") or "").lower(), "S_TEXT/UTF8"
        )
        tracks_data += _ebml_subtitle_track_entry(
            track_num=next_num,
            codec_mkv=mkv_codec,
            lang=(st.get("language") or "und")[:3],
        )
        next_num += 1

    tracks_el = _ebml_el(b'\x16\x54\xAE\x6B', tracks_data)

    # Minimal empty Cluster (Timecode=0, no frames).
    # Required so ffprobe / Plex scanner detect the video stream.
    cluster_data = _ebml_el(b'\xE7', _ebml_uint(0))   # Timecode = 0
    cluster_el   = _ebml_el(b'\x1F\x43\xB6\x75', cluster_data)

    # Segment with known size (header + tracks + cluster)
    segment_body = info_el + tracks_el + cluster_el
    segment = b'\x18\x53\x80\x67' + b'\x01\xFF\xFF\xFF\xFF\xFF\xFF\xFF' + segment_body

    return ebml_header + segment


def _spore_stub_dir(strm_path: Path) -> Path:
    """Return the Spore stub directory for a given .strm path.

    Mirrors the relative path under MEDIA_PATH into SPORE_MEDIA_PATH so that
    Plex can be pointed at a clean directory containing only .mkv stubs.
    Example: /data/media/movies/Foo/Foo.strm -> /data/plex-media/movies/Foo/
    """
    media_root = Path(MEDIA_PATH)
    spore_root = Path(SPORE_MEDIA_PATH)
    try:
        rel = strm_path.parent.relative_to(media_root)
        return spore_root / rel
    except ValueError:
        # strm_path is not under MEDIA_PATH (relative path or different prefix).
        # Try to preserve the movies/ or series/ subdir from the path components.
        parts = strm_path.parts
        for anchor in ("movies", "series"):
            if anchor in parts:
                idx = parts.index(anchor)
                return spore_root / Path(*parts[idx:-1])
        return spore_root / strm_path.parent.name


def _write_spore_stubs(strm_path: Path, token: str,
                        title: str, quality: str | None,
                        size_gb: float | None) -> None:
    """Write .mkv stub and .minfo sidecar into SPORE_MEDIA_PATH.

    Mirrors the media directory structure so Plex can be pointed at
    SPORE_MEDIA_PATH as a clean library root (no .strm files, no artwork).
    Jellyfin keeps using MEDIA_PATH with .strm files unchanged.
    """
    if not settings.get("SPORE_ENABLED", cfg.SPORE_ENABLED):
        return

    stub_dir   = _spore_stub_dir(strm_path)
    mkv_path   = stub_dir / (strm_path.stem + ".mkv")
    minfo_path = stub_dir / (strm_path.stem + ".minfo")

    if mkv_path.exists() and minfo_path.exists():
        return

    try:
        stub_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        log.warning("Spore: could not create stub dir %s: %s", stub_dir, exc)
        return

    # Write stub .mkv
    # Try to get a real duration from TMDB so Plex shows the correct runtime
    # without needing to probe the CDN file first. Audio/subtitle tracks are
    # still placeholder (EAC3 6ch) until the first Jellyfin play triggers
    # catbox materialization and CDN probe -- that's correct catbox behaviour.
    if not mkv_path.exists():
        try:
            duration_sec = 7200.0
            try:
                vi = db.get_virtual_item(token)
                imdb_id = vi.get("imdb_id") if vi else None
                if imdb_id:
                    import tmdb as _tmdb
                    season  = vi.get("season")
                    episode = vi.get("episode")
                    if season and episode:
                        dur = _tmdb.get_episode_runtime_sec(imdb_id, season, episode)
                    else:
                        dur = _tmdb.get_movie_runtime_sec(imdb_id)
                    if dur and dur > 60:
                        duration_sec = dur
                        log.debug("Spore: TMDB duration %.0fs for %s", duration_sec, title)
            except Exception as _e:
                log.debug("Spore: TMDB duration lookup failed for %s: %s", title, _e)

            stub = make_stub_mkv(title, quality, duration_sec=duration_sec)
            mkv_path.write_bytes(stub)
            log.debug("Spore: wrote stub MKV %s (%d bytes, quality=%s dur=%.0fs)",
                      mkv_path.name, len(stub), quality or "?", duration_sec)
        except Exception as exc:
            log.warning("Spore: could not write stub MKV %s: %s", mkv_path, exc)
            return

    # Write .minfo (token + cdn_size)
    if not minfo_path.exists():
        try:
            size_bytes = int((size_gb or 0.0) * 1_000_000_000)
            minfo_path.write_text(
                f"token={token}\nsize={size_bytes}\n", encoding="utf-8"
            )
            log.debug("Spore: wrote .minfo %s (token=%s size=%d)",
                      minfo_path.name, token, size_bytes)
        except Exception as exc:
            log.warning("Spore: could not write .minfo %s: %s", minfo_path, exc)


def _delete_spore_stubs(strm_path: Path) -> None:
    """Remove .mkv stub and .minfo for a given .strm path (if they exist)."""
    stub_dir = _spore_stub_dir(strm_path)
    for ext in (".mkv", ".minfo"):
        stub = stub_dir / (strm_path.stem + ext)
        try:
            stub.unlink(missing_ok=True)
        except Exception as exc:
            log.warning("Spore: could not delete stub %s: %s", stub, exc)
    try:
        stub_dir.rmdir()
    except OSError:
        pass


def backfill_spore_stubs() -> dict:
    """Generate missing Spore stubs for all existing virtual_items.

    Safe to call multiple times - skips items that already have both
    .mkv and .minfo in SPORE_MEDIA_PATH.
    Returns {total, created, skipped, errors}.
    """
    items = db.get_all_virtual_items()
    total = len(items)
    created = skipped = errors = 0

    for item in items:
        strm_path_str = item.get("strm_path")
        if not strm_path_str:
            skipped += 1
            continue

        strm_path  = Path(strm_path_str)
        stub_dir   = _spore_stub_dir(strm_path)
        mkv_path   = stub_dir / (strm_path.stem + ".mkv")
        minfo_path = stub_dir / (strm_path.stem + ".minfo")

        if mkv_path.exists() and minfo_path.exists():
            skipped += 1
            continue

        try:
            _write_spore_stubs(
                strm_path,
                item["token"],
                item.get("title") or strm_path.stem,
                item.get("quality"),
                item.get("size_gb"),
            )
            created += 1
        except Exception as exc:
            log.warning("Spore backfill: failed for %s: %s", strm_path.name, exc)
            errors += 1

    log.info("Spore backfill: total=%d created=%d skipped=%d errors=%d",
             total, created, skipped, errors)
    return {"total": total, "created": created, "skipped": skipped, "errors": errors}


def regenerate_spore_stubs(token: str | None = None) -> dict:
    """Force-regenerate stub MKVs for all items (or a single token).

    Deletes and rewrites the .mkv stub so codec metadata is corrected.
    Does NOT touch .minfo files (token/size stay unchanged).
    Returns {total, regenerated, skipped, errors}.
    """
    if token:
        item = db.get_virtual_item(token)
        items = [item] if item else []
    else:
        items = db.get_all_virtual_items()

    total = len(items)
    regenerated = skipped = errors = 0

    for item in items:
        strm_path_str = item.get("strm_path")
        if not strm_path_str:
            skipped += 1
            continue

        strm_path = Path(strm_path_str)
        stub_dir  = _spore_stub_dir(strm_path)
        mkv_path  = stub_dir / (strm_path.stem + ".mkv")

        try:
            saved = db.load_spore_tracks(item["token"]) or {}
            # audio_tracks=None: always use TrueHD 8ch placeholder so Plex never
            # Direct Plays the stub. Real audio info is in the DB for metadata only.
            sub_tracks = [
                {"codec": s.get("codec_name", "subrip"),
                 "language": (s.get("tags") or {}).get("language", "und")[:3]}
                for s in saved.get("subs", [])
            ] or None
            dur = float(saved.get("duration_s") or 0) or 7200.0
            v_extra_hex = saved.get("video_extradata_hex") or ""
            try:
                v_extra = bytes.fromhex(v_extra_hex) if v_extra_hex else None
            except ValueError:
                log.debug("Spore regenerate: invalid extradata hex for %s, skipping", strm_path.name)
                v_extra = None
            stub = make_stub_mkv(
                item.get("title") or strm_path.stem,
                item.get("quality"),
                duration_sec=dur,
                audio_tracks=None,
                subtitle_tracks=sub_tracks,
            )
            stub_dir.mkdir(parents=True, exist_ok=True)
            mkv_path.write_bytes(stub)
            log.info("Spore: regenerated stub %s (quality=%s subs=%d)",
                     mkv_path.name, item.get("quality") or "?", len(sub_tracks or []))
            regenerated += 1
        except Exception as exc:
            log.warning("Spore regenerate: failed for %s: %s", strm_path.name, exc)
            errors += 1

    log.info("Spore regenerate: total=%d regenerated=%d skipped=%d errors=%d",
             total, regenerated, skipped, errors)
    return {"total": total, "regenerated": regenerated, "skipped": skipped, "errors": errors}


def probe_pending_stubs() -> dict:
    """Background job: probe CDN files for Plex stubs that have no track info yet.

    Only probes items already present in TorBox library -- no torrents are added.
    This preserves the catbox lazy principle: TorBox is only touched when a user
    plays something in Jellyfin (catbox materialization) or CATBOX_PRELOAD runs.

    Duration is set via TMDB at stub creation time (_write_spore_stubs), so no
    probe is needed just for runtime. This job fills in real audio/sub tracks
    for items that happen to still be cached in TorBox (e.g. recently played).

    Uses build_fsh=False: just ffprobe, no 32MB download per file.
    Fast-start cache is built on first actual Plex play.
    """
    if not settings.get("SPORE_ENABLED", cfg.SPORE_ENABLED):
        return {"skipped": "SPORE_ENABLED=false"}

    import catbox as _catbox

    items = db.get_unprobed_spore_items()
    if not items:
        log.debug("Probe pending: nothing to do")
        return {"probed": 0, "skipped": 0, "queued_preload": 0, "errors": 0}

    log.info("Probe pending: %d stubs without track info", len(items))

    # Group by info_hash to avoid duplicate TorBox API lookups
    by_hash: dict[str, list[dict]] = {}
    for item in items:
        h = (item.get("info_hash") or "").lower()
        if h:
            by_hash.setdefault(h, []).append(item)

    probed = skipped = errors = 0

    for info_hash, hash_items in by_hash.items():
        try:
            ready = torbox_mod.find_by_hash(info_hash)
            if not ready or not torbox_mod._is_ready(ready):
                skipped += len(hash_items)
                continue

            torrent_id = ready.get("id")
            files = ready.get("files") or []
            if not files or not torrent_id:
                fresh = torbox_mod.find_by_hash(info_hash, force_refresh=True)
                if fresh:
                    files = fresh.get("files") or []
                    torrent_id = fresh.get("id") or torrent_id
            if not files or not torrent_id:
                skipped += len(hash_items)
                continue

            for vi in hash_items:
                token   = vi.get("token")
                season  = vi.get("season")
                episode = vi.get("episode")
                if not token:
                    skipped += 1
                    continue
                if db.load_spore_tracks(token):   # probed in the meantime
                    skipped += 1
                    continue

                if season and episode:
                    f = _pick_episode_file(files, season, episode)
                    if not f:
                        log.debug("Probe pending: no file for S%02dE%02d token=%s",
                                  season, episode, token)
                        skipped += 1
                        continue
                    file_id = f.get("id")
                else:
                    main = _pick_main_movie_file(files)
                    file_id = (main or files[0]).get("id")

                cdn_url = _get_stream_url(torrent_id, file_id)
                if not cdn_url:
                    skipped += 1
                    continue

                _catbox.cache_url(token, cdn_url)
                _preload_spore(cdn_url, token, build_fsh=False)
                probed += 1
                time.sleep(0.3)

        except Exception as exc:
            log.warning("Probe pending: error for hash %s: %s", info_hash, exc)
            errors += len(hash_items)

    log.info("Probe pending done: probed=%d skipped=%d errors=%d",
             probed, skipped, errors)
    return {"probed": probed, "skipped": skipped, "errors": errors}


def update_stub_from_probe(token: str, audio_streams: list[dict],
                            subtitle_streams: list[dict],
                            duration_s: float | None = None) -> bool:
    """Rewrite the stub MKV for token with real audio and subtitle tracks from ffprobe.

    Called after build_and_cache() completes so subsequent Plex analyses show
    the correct track layout (enabling audio switching and subtitle selection).
    Returns True if the stub was updated.
    """
    item = db.get_virtual_item(token)
    if not item:
        return False
    strm_path_str = item.get("strm_path")
    if not strm_path_str:
        return False

    strm_path = Path(strm_path_str)
    mkv_path  = _spore_stub_dir(strm_path) / (strm_path.stem + ".mkv")
    if not mkv_path.parent.exists():
        return False

    # Write real audio tracks so Plex shows the correct languages and the user
    # can switch between e.g. Dutch / English / Italian in the Plex UI.
    # Stream order matches the CDN file order, so Plex's -map 0:N references
    # are correctly forwarded to the CDN file by the transcoder wrapper.
    audio_tracks = [
        {
            "codec":       (s.get("codec_name") or "eac3").lower(),
            "language":    (s.get("tags") or {}).get("language", "und")[:3],
            "channels":    s.get("channels") or 2,
            "sample_rate": int(s.get("sample_rate") or 48000),
        }
        for s in audio_streams
    ] or None

    subtitle_tracks = [
        {
            "codec":    s.get("codec_name", "subrip"),
            "language": (s.get("tags") or {}).get("language", "und")[:3],
        }
        for s in subtitle_streams
    ]

    # Write cdn_audio_codec to .minfo so the transcoder wrapper can inject a
    # native (non-EAE) decoder hint, preventing EAE input-decode timeouts on
    # heavy sessions (e.g. Shield TV + VAAPI video transcode).
    cdn_codec = (audio_streams[0].get("codec_name") or "").lower() if audio_streams else ""
    if cdn_codec:
        minfo_path = mkv_path.parent / (strm_path.stem + ".minfo")
        try:
            if minfo_path.exists():
                lines = minfo_path.read_text(encoding="utf-8").splitlines()
                lines = [l for l in lines if not l.startswith("cdn_audio_codec=")]
                lines.append(f"cdn_audio_codec={cdn_codec}")
                minfo_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                log.info("Spore: cdn_audio_codec=%s saved to .minfo for token=%s", cdn_codec, token)
        except Exception as exc:
            log.warning("Spore: could not write cdn_audio_codec to .minfo for token=%s: %s", token, exc)

    try:
        stub = make_stub_mkv(
            item.get("title") or strm_path.stem,
            item.get("quality"),
            duration_sec=duration_s or 7200.0,
            audio_tracks=audio_tracks,
            subtitle_tracks=subtitle_tracks or None,
            # video_codec_private omitted: updated via update_stub_from_probe
        )
        mkv_path.write_bytes(stub)
        log.info(
            "Spore: updated stub for token=%s with %d audio + %d subs",
            token, len(audio_streams), len(subtitle_tracks),
        )
        # Also update the NFO sidecar so Plex sees the real codec / language info
        # This applies to both stub MKV library and .strm library
        try:
            quality = item.get("quality")
            update_nfo_streamdetails(strm_path, quality,
                                     audio_tracks or [], subtitle_tracks or [])
        except Exception as _nfo_exc:
            log.debug("Spore: NFO update failed for token=%s: %s", token, _nfo_exc)
        return True
    except Exception as exc:
        log.warning("Spore: stub update failed for token=%s: %s", token, exc)
        return False


# =============================================================================
# JELLYFIN .strm  --  batch write / repair / cleanup
# Alles hieronder schrijft of herstelt Jellyfin .strm bestanden.
# Plex Spore stubs (.mkv / .minfo) worden hier NIET aangeraakt.
# =============================================================================

def _write_strm(path: Path, url: str) -> bool:
    """Write .strm file only if it doesn't exist. Returns True if a new file was written."""
    if path.exists():
        return False
    # Fuzzy duplicate check: skip if any existing sibling folder normalizes to the same title.
    # Catches "The Minecraft Movie (2025)" vs "Minecraft Movie The (2025)", case differences, etc.
    parent = path.parent.parent  # movies/ or series/
    norm = _norm_title(path.parent.name)
    if parent.is_dir():
        for existing in parent.iterdir():
            if existing.is_dir() and existing != path.parent and _norm_title(existing.name) == norm:
                if any(existing.glob("*.strm")):
                    log.info("Skipping duplicate strm %s  -  already have %s", path.parent.name, existing.name)
                    return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(url, encoding='utf-8')
        log.info("Created .strm: %s", path)
        return True
    except Exception as exc:
        log.warning("Could not write %s: %s", path, exc)
        return False


def _resolve_url(item: dict, file_id: int, file_name: str, info: dict, media_type: str) -> str | None:
    """Return the URL to write into a .strm file.
    In Catbox mode this is a proxy URL pointing at /stream/<token>.
    Otherwise it is the direct TorBox CDN URL.
    """
    torrent_id = item.get("id")
    if settings.get("CATBOX_MODE", False):
        import catbox
        magnet = item.get("magnet") or f"magnet:?xt=urn:btih:{item.get('hash')}"
        title = f"{info.get('title','')} ({info['year']})" if info.get("year") else info.get("title", file_name)
        token = catbox.register(
            info_hash=(item.get("hash") or "").lower(),
            magnet=magnet,
            title=title,
            media_type=media_type,
            torbox_id=torrent_id,
            file_id=file_id,
        )
        return catbox.proxy_url(token)
    return _get_stream_url(torrent_id, file_id)


def process_torrent(item: dict) -> int:
    """Create .strm files for all video files in a ready torrent. Returns new file count."""
    torrent_id = item.get('id')
    torrent_name = _clean_torrent_name(item.get('name') or '')
    files = item.get('files') or []

    if not torbox_mod._is_ready(item):
        return 0

    # In Catbox mode: if this hash is already registered and the strm still exists on disk,
    # skip entirely  -  avoids creating a second folder with the torrent-parsed title
    # when the movie was originally added with the TMDB title.
    if settings.get("CATBOX_MODE", False):
        info_hash = (item.get('hash') or '').lower()
        if info_hash:
            existing = db.get_virtual_item_by_hash(info_hash)
            if existing and existing.get('strm_path') and Path(existing['strm_path']).exists():
                log.debug("process_torrent: %s already has strm at %s  -  skipping",
                          torrent_name, existing['strm_path'])
                return 0

    is_series = bool(_EP_RE.search(_clean(torrent_name)) or
                     re.search(r'\bS\d{1,2}\b', torrent_name, re.IGNORECASE))
    if not is_series and _TRAILER_RE.search(_clean(torrent_name)):
        log.debug("Skipping trailer torrent %s (%s)", torrent_id, torrent_name)
        return 0
    if is_series:
        video_files = [f for f in files if _is_video(f.get('name') or '') and not _is_trailer(f)]
    else:
        main_file = _pick_main_movie_file(files)
        video_files = [main_file] if main_file else []

    if not video_files:
        log.debug("No suitable video files in torrent %s (%s)", torrent_id, torrent_name)
        return 0

    written = 0
    for f in video_files:
        file_id = f.get('id')
        file_name = f.get('name') or ''
        info = _parse_info(torrent_name, file_name)
        if info is None:
            log.warning("Cannot determine placement: torrent=%r file=%r", torrent_name, file_name)
            continue
        path = _strm_path(info)
        if path.exists():
            continue
        url = _resolve_url(item, file_id, file_name, info, info['type'] if info['type'] == 'movie' else 'series')
        if not url:
            continue
        if _write_strm(path, url):
            written += 1

    return written


def create_strm_for_torrent(torrent_id: int, title: str, media_type: str,
                             imdb_id: str | None = None, tmdb_id: int | None = None) -> int:
    """
    Immediately create .strm file(s) for a just-added torrent.
    For movies: uses file_id=0 (fast, ~1 API call).
    For series: fetches the torrent's file list from mylist and creates per-episode .strm files.
    Returns count of new files written.
    """
    item = torbox_mod.find_by_id(torrent_id)
    if not item:
        log.warning("Torrent %s not found in mylist for strm creation", torrent_id)
        return 0

    if media_type == 'movie':
        main_file = _pick_main_movie_file(item.get('files') or [])
        if not main_file:
            log.warning("No suitable video file in torrent %s", torrent_id)
            return 0
        yr = _YEAR_RE.search(title)
        year = int(yr.group(1)) if yr else None
        clean_title = _safe(title[:yr.start()].strip() if yr else title)
        folder = f"{clean_title} ({year})" if year else clean_title
        path = Path(MEDIA_PATH) / 'movies' / folder / f"{folder}.strm"
        info = {'type': 'movie', 'title': clean_title, 'year': year}
        url = _resolve_url(item, main_file['id'], main_file.get('name', ''), info, 'movie')
        if not url:
            return 0
        written = _write_strm(path, url)
        if written and (imdb_id or tmdb_id):
            _write_nfo(path, imdb_id, tmdb_id)
        return 1 if written else 0

    return process_torrent(item)


def create_series_strms_from_files(torrent_name: str, files_with_urls: list) -> int:
    """For a season-pack torrent on any debrid provider, write per-episode .strm
    files. files_with_urls is a list of (file_dict_with_path_and_size, direct_url).
    Returns count of new files written."""
    written = 0
    for f, url in files_with_urls:
        file_name = (f.get("path") or f.get("name") or "").lstrip("/").split("/")[-1]
        info = _parse_info(torrent_name, file_name)
        if not info or info["type"] != "episode":
            log.debug("Skip non-episode file: %s", file_name)
            continue
        path = _strm_path(info)
        if path.exists():
            continue
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(url, encoding="utf-8")
            log.info("Created series .strm: %s", path)
            written += 1
        except Exception as exc:
            log.warning("Could not write series .strm %s: %s", path, exc)
    return written


def create_episode_strm_from_url(title: str, season: int, episode: int,
                                   url: str) -> Path | None:
    """Write a single-episode .strm at series/{Title}/Season XX/{Title} SnnEmm.strm."""
    if not url:
        return None
    clean_title = _safe(title)
    if not clean_title:
        return None
    season_folder = f"Season {season:02d}"
    file_name = f"{clean_title} S{season:02d}E{episode:02d}.strm"
    path = Path(MEDIA_PATH) / "series" / clean_title / season_folder / file_name
    if path.exists():
        return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(url, encoding="utf-8")
        log.info("Created episode .strm: %s", path)
        return path
    except Exception as exc:
        log.warning("Could not write episode .strm %s: %s", path, exc)
        return None


def create_movie_strm_from_url(title: str, url: str) -> Path | None:
    """Write a movie .strm pointing at a direct CDN URL (e.g. RealDebrid).
    Parses year from the title and builds the standard movies/Title (Year)/Title (Year).strm
    path. Returns the written path, or None on failure."""
    if not url:
        return None
    yr = _YEAR_RE.search(title)
    year = int(yr.group(1)) if yr else None
    clean_title = _safe(title[:yr.start()].strip() if yr else title)
    folder = f"{clean_title} ({year})" if year else clean_title
    path = Path(MEDIA_PATH) / "movies" / folder / f"{folder}.strm"
    if path.exists():
        return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(url, encoding="utf-8")
        log.info("Created RD .strm: %s", path)
        return path
    except Exception as exc:
        log.warning("Could not write RD .strm %s: %s", path, exc)
        return None


def _run_once_catbox() -> int:
    """Catbox mode: rebuild any .strm files that are missing from disk.

    virtual_items is the source of truth  -  torrents are not in TorBox when
    idle-released, so scanning mylist would find nothing.
    """
    import catbox
    items = db.get_all_virtual_items()
    recreated = 0
    for item in items:
        strm_path = item.get("strm_path")
        if not strm_path:
            continue
        path = Path(strm_path)
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if _write_strm(path, catbox.proxy_url(item["token"])):
            log.info("Recreated missing catbox .strm: %s", path.name)
            recreated += 1
    log.info("strm_generator catbox: %d missing .strm file(s) recreated from virtual_items", recreated)
    return recreated


def run_once() -> int:
    """Create any missing .strm files. In catbox mode uses virtual_items DB as
    source of truth; otherwise scans TorBox mylist."""
    if settings.get("CATBOX_MODE", False):
        return _run_once_catbox()
    log.info("strm_generator: scanning TorBox mylist")
    try:
        torrents = torbox_mod.list_torrents()
    except Exception as exc:
        log.error("strm_generator: mylist failed: %s", exc)
        return 0
    total = sum(process_torrent(t) for t in torrents)
    log.info("strm_generator: %d new .strm file(s) created", total)
    return total


def run_and_refresh() -> None:
    """Run strm generation and trigger Jellyfin scan if any new files were created."""
    new_files = run_once()
    import nfo_generator
    nfo_generator.generate_all()
    if new_files > 0:
        jellyfin.refresh_library()
    # Self-healing: proactive health probe of a small sample of existing strms.
    try:
        _self_heal_sample()
    except Exception as exc:
        log.debug("Self-heal sample failed: %s", exc)


def migrate_to_canonical_names() -> dict:
    """One-time migration: rename all movie folders to TMDB canonical names
    and merge duplicate folders that share the same imdb_id.

    Acquires _maintenance_lock so repair cannot run simultaneously.

    Rules:
    - imdb_id is the key (read from .nfo files)
    - Canonical name = TMDB title + year; falls back to current name if TMDB fails
    - Multiple folders for same imdb_id → keep the one with .strm (or most files),
      delete the rest
    - Updates virtual_items.strm_path in DB for any renamed/merged folders

    Returns: {scanned, renamed, merged, skipped, errors, no_imdb}
    """
    import re as _re
    import shutil

    if not _maintenance_lock.acquire(blocking=False):
        log.warning("migrate_to_canonical_names: maintenance already running  -  skipping")
        return {"scanned": 0, "renamed": 0, "merged": 0, "skipped": 0, "errors": 0, "no_imdb": 0}
    try:
        return _migrate_to_canonical_names_locked()
    finally:
        _maintenance_lock.release()


def _migrate_to_canonical_names_locked() -> dict:
    import re as _re
    import shutil

    root = Path(MEDIA_PATH) / "movies"
    if not root.exists():
        return {"scanned": 0, "renamed": 0, "merged": 0, "skipped": 0, "errors": 0, "no_imdb": 0}

    def _read_imdb(folder: Path) -> str | None:
        for nfo in folder.glob("*.nfo"):
            try:
                text = nfo.read_text(encoding="utf-8", errors="ignore")
                m = _re.search(
                    r"<imdbid>(tt\d+)</imdbid>"
                    r"|<uniqueid[^>]*type=['\"]imdb['\"][^>]*>(tt\d+)</uniqueid>"
                    r"|(tt\d{7,})",
                    text,
                )
                if m:
                    return next(g for g in m.groups() if g)
            except Exception:
                pass
        return None

    def _do_rename(old: Path, new: Path) -> bool:
        try:
            old.rename(new)
            # Update DB strm_path: any virtual_item pointing into old folder
            db.update_virtual_strm_path_prefix(str(old), str(new))
            # Rename .strm/.nfo files inside that still use the old folder stem
            old_stem = old.name
            new_stem = new.name
            for suffix in (".strm", ".nfo"):
                old_file = new / (old_stem + suffix)
                if old_file.exists():
                    new_file = new / (new_stem + suffix)
                    if not new_file.exists():
                        old_file.rename(new_file)
                        if suffix == ".strm":
                            db.update_virtual_strm_path_prefix(str(old_file), str(new_file))
            log.info("migrate: renamed '%s' → '%s'", old.name, new.name)
            return True
        except Exception as exc:
            log.error("migrate: rename failed %s → %s: %s", old.name, new.name, exc)
            return False

    def _do_delete(folder: Path) -> None:
        try:
            shutil.rmtree(folder)
            log.info("migrate: deleted duplicate '%s'", folder.name)
        except Exception as exc:
            log.error("migrate: delete failed %s: %s", folder.name, exc)

    # Build map: imdb_id → list of folders
    by_imdb: dict[str, list[Path]] = {}
    no_imdb_count = 0
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        imdb_id = _read_imdb(folder)
        if imdb_id:
            by_imdb.setdefault(imdb_id.lower(), []).append(folder)
        else:
            no_imdb_count += 1

    scanned = sum(len(v) for v in by_imdb.values()) + no_imdb_count
    renamed = merged = skipped = errors = 0

    for imdb_id, folders in by_imdb.items():
        try:
            canonical = _canonical_movie_folder(imdb_id)
            if not canonical:
                log.debug("migrate: no canonical name for %s  -  skipping", imdb_id)
                skipped += len(folders)
                continue

            canonical_path = root / canonical

            if len(folders) == 1:
                folder = folders[0]
                if folder.resolve() == canonical_path.resolve():
                    skipped += 1
                    continue
                if canonical_path.exists():
                    log.warning("migrate: target '%s' already exists for %s  -  skipping",
                                canonical, imdb_id)
                    skipped += 1
                    continue
                if _do_rename(folder, canonical_path):
                    renamed += 1
                else:
                    errors += 1
            else:
                # Multiple folders → pick best (has .strm > most files), delete rest
                def _score(f: Path) -> int:
                    return int(any(f.glob("*.strm"))) * 1000 + len(list(f.iterdir()))

                ordered = sorted(folders, key=_score, reverse=True)
                keep = ordered[0]

                # Rename best folder to canonical if needed
                if keep.resolve() != canonical_path.resolve():
                    if canonical_path.exists():
                        # canonical already exists  -  it might be one of the others
                        if canonical_path in ordered:
                            keep = canonical_path
                        else:
                            log.warning("migrate: canonical '%s' exists but isn't in group  -  skipping",
                                        canonical)
                            skipped += len(folders)
                            continue
                    else:
                        if not _do_rename(keep, canonical_path):
                            errors += 1
                            continue
                        keep = canonical_path
                        renamed += 1

                # Delete duplicates
                for dup in ordered[1:]:
                    if dup.resolve() == keep.resolve():
                        continue
                    _do_delete(dup)
                    merged += 1

        except Exception as exc:
            log.error("migrate: error processing %s: %s", imdb_id, exc)
            errors += 1

    log.info("migrate_to_canonical_names: scanned=%d renamed=%d merged=%d skipped=%d errors=%d no_imdb=%d",
             scanned, renamed, merged, skipped, errors, no_imdb_count)
    return {
        "scanned": scanned,
        "renamed": renamed,
        "merged": merged,
        "skipped": skipped,
        "errors": errors,
        "no_imdb": no_imdb_count,
    }


def repair_expired_strms(media_type: str = "movie") -> dict:
    """Find and fix all unplayable movie entries.

    Three kinds of breakage handled:
    1. Movie folder exists but has NO .strm file at all (NFO/poster present,
       added via generate-nfos before processor ran or after .strm was lost).
    2. Direct TorBox CDN URL in .strm  -  expired after ~24h.
    3. Catbox proxy URL whose token is NOT in virtual_items DB  -  404 on play.

    Repair strategy for each broken item:
      a. If a virtual_item exists for that imdb_id → write/rewrite the .strm
         to point at the correct catbox proxy URL.
      b. Otherwise → delete the broken .strm (if any) and requeue via processor
         so it gets a fresh catbox token on the next pass.
    Returns a summary dict with counts.
    """
    if not _maintenance_lock.acquire(blocking=False):
        log.warning("repair_expired_strms: maintenance already running  -  skipping")
        return {"scanned": 0, "ok": 0, "missing_strm": 0, "orphaned_tokens": 0,
                "relinked": 0, "requeued": 0, "skipped": 0}
    try:
        return _repair_expired_strms_locked(media_type)
    finally:
        _maintenance_lock.release()


def _repair_expired_strms_locked(media_type: str = "movie") -> dict:
    import re as _re
    import catbox as _catbox
    catbox_base = _catbox.catbox_host().rstrip("/") + "/stream/"

    media = Path(MEDIA_PATH)
    sub = "movies" if media_type == "movie" else "series"
    root = media / sub
    if not root.is_dir():
        return {"scanned": 0, "ok": 0, "missing_strm": 0, "orphaned_tokens": 0,
                "relinked": 0, "requeued": 0, "skipped": 0}

    scanned = ok = missing = orphaned = relinked = requeued = skipped = 0

    def _nfo_imdb(folder: Path) -> str | None:
        for nfo in folder.glob("*.nfo"):
            try:
                text = nfo.read_text(encoding="utf-8", errors="ignore")
                m = _re.search(
                    r"<imdbid>(tt\d+)</imdbid>"
                    r"|<uniqueid[^>]*type=['\"]imdb['\"][^>]*>(tt\d+)</uniqueid>"
                    r"|(tt\d{7,})",
                    text,
                )
                if m:
                    return next(g for g in m.groups() if g)
            except Exception:
                pass
        return None

    def _requeue(imdb_id: str, title: str, strm_path: Path | None) -> None:
        """Delete broken .strm (if any) and kick off processor."""
        if strm_path and strm_path.exists():
            try:
                strm_path.unlink()
            except Exception:
                pass
        try:
            import processor as _proc
            from webhook_parser import MediaRequest as _MR
            import threading as _t
            req = _MR(title=title, media_type=media_type, imdb_id=imdb_id, seasons=[])
            _t.Thread(target=_proc.process, args=(req,),
                      name=f"repair-{imdb_id}", daemon=True).start()
        except Exception as exc:
            log.warning("repair_strms: requeue failed for %s: %s", imdb_id, exc)

    def _relink(imdb_id: str, strm_path: Path) -> bool:
        """Write/rewrite strm_path to the catbox proxy URL for imdb_id. Returns True on success."""
        items = db.get_virtual_items_by_imdb(imdb_id, media_type)
        if not items:
            return False
        item = next((i for i in items if i.get("strm_path") == str(strm_path)), items[0])
        new_url = _catbox.proxy_url(item["token"])
        try:
            strm_path.parent.mkdir(parents=True, exist_ok=True)
            strm_path.write_text(new_url, encoding="utf-8")
            log.info("repair_strms: wrote %s → token %s", strm_path.name, item["token"])
            return True
        except Exception as exc:
            log.warning("repair_strms: could not write %s: %s", strm_path, exc)
            return False

    # ── Pass 1: folders with NO .strm file ────────────────────────────────────
    for movie_dir in root.iterdir():
        if not movie_dir.is_dir():
            continue
        strms = list(movie_dir.glob("*.strm"))
        if strms:
            continue  # has at least one .strm  -  handled in pass 2
        # Skip if a sibling folder with the same normalised title already has a .strm.
        norm = _norm_title(movie_dir.name)
        if any(
            sib.is_dir() and sib != movie_dir
            and _norm_title(sib.name) == norm
            and any(sib.glob("*.strm"))
            for sib in root.iterdir()
        ):
            log.debug("repair_strms: skipping %s  -  duplicate of sibling with .strm", movie_dir.name)
            skipped += 1
            continue
        # No .strm  -  check if there's a .nfo we can use to requeue
        imdb_id = _nfo_imdb(movie_dir)
        if not imdb_id:
            log.debug("repair_strms: no .nfo imdb_id in %s  -  skipping", movie_dir.name)
            skipped += 1
            continue
        missing += 1
        expected_strm = movie_dir / f"{movie_dir.name}.strm"
        if _relink(imdb_id, expected_strm):
            relinked += 1
        else:
            log.info("repair_strms: no virtual_item for %s  -  requeuing", movie_dir.name)
            _requeue(imdb_id, movie_dir.name, None)
            requeued += 1

    # ── Pass 2: existing .strm files that are broken ──────────────────────────
    for strm_path in root.rglob("*.strm"):
        scanned += 1
        try:
            url = strm_path.read_text(encoding="utf-8").strip()
        except Exception:
            skipped += 1
            continue

        # Valid catbox proxy URL  -  verify token is in DB.
        if url.startswith(catbox_base):
            m = _re.search(r"/stream/([a-f0-9]{16})$", url)
            token = m.group(1) if m else None
            if token and db.get_virtual_item(token):
                ok += 1
                continue
            orphaned += 1
            log.warning("repair_strms: orphaned token %s in %s", token, strm_path.name)
            # Fall through to repair below.

        movie_folder = strm_path.parent
        imdb_id = _nfo_imdb(movie_folder)
        if not imdb_id:
            log.warning("repair_strms: no imdb_id for %s  -  skipping", strm_path)
            skipped += 1
            continue

        if _relink(imdb_id, strm_path):
            relinked += 1
        else:
            _requeue(imdb_id, movie_folder.name, strm_path)
            requeued += 1

    log.info(
        "repair_strms: missing=%d ok=%d orphaned=%d relinked=%d requeued=%d skipped=%d",
        missing, ok, orphaned, relinked, requeued, skipped,
    )
    return {
        "scanned": scanned, "ok": ok, "missing_strm": missing,
        "orphaned_tokens": orphaned, "relinked": relinked,
        "requeued": requeued, "skipped": skipped,
    }


def cleanup_duplicate_strms() -> dict:
    """Remove extra .strm (and matching .nfo) files from folders that have more than one.
    Keeps the .strm whose stem matches the folder name; falls back to the one whose
    token has an imdb_id set in virtual_items."""
    if not _maintenance_lock.acquire(blocking=False):
        log.warning("cleanup_duplicate_strms: maintenance already running  -  skipping")
        return {"scanned": 0, "cleaned": 0, "skipped": 0}
    try:
        return _cleanup_duplicate_strms_locked()
    finally:
        _maintenance_lock.release()


def _cleanup_duplicate_strms_locked() -> dict:
    root = Path(MEDIA_PATH) / "movies"
    if not root.exists():
        return {"scanned": 0, "cleaned": 0, "skipped": 0}

    scanned = cleaned = skipped = 0

    for movie_dir in root.iterdir():
        if not movie_dir.is_dir():
            continue
        strms = list(movie_dir.glob("*.strm"))
        if len(strms) <= 1:
            continue

        scanned += 1
        canonical_stem = movie_dir.name

        # Prefer the .strm whose stem matches the folder name
        keep = next((s for s in strms if s.stem == canonical_stem), None)

        # Fallback: prefer one whose token has imdb_id in DB
        if keep is None:
            for s in strms:
                try:
                    token = s.read_text(encoding="utf-8").strip().rstrip("/").split("/")[-1]
                    item = db.get_virtual_item(token)
                    if item and item.get("imdb_id"):
                        keep = s
                        break
                except Exception:
                    pass

        # Last resort: keep alphabetically first
        if keep is None:
            keep = sorted(strms)[0]

        # If keep doesn't have the canonical name, rename it and update DB
        if keep.stem != canonical_stem:
            new_path = keep.parent / (canonical_stem + ".strm")
            if not new_path.exists():
                try:
                    keep.rename(new_path)
                    db.update_virtual_strm_path_prefix(str(keep), str(new_path))
                    nfo_old = keep.with_suffix(".nfo")
                    nfo_new = new_path.with_suffix(".nfo")
                    if nfo_old.exists() and not nfo_new.exists():
                        nfo_old.rename(nfo_new)
                    keep = new_path
                except Exception as exc:
                    log.warning("cleanup_duplicate_strms: could not rename %s: %s", keep, exc)
                    skipped += 1
                    continue

        # Remove all other .strm files and their .nfo sidecars
        for s in list(movie_dir.glob("*.strm")):
            if s.resolve() == keep.resolve():
                continue
            nfo = s.with_suffix(".nfo")
            if nfo.exists():
                try:
                    nfo.unlink()
                except Exception as exc:
                    log.warning("cleanup_duplicate_strms: could not remove %s: %s", nfo, exc)
            try:
                s.unlink()
                log.info("cleanup_duplicate_strms: removed extra strm %s in %s", s.name, movie_dir.name)
                cleaned += 1
            except Exception as exc:
                log.warning("cleanup_duplicate_strms: could not remove %s: %s", s, exc)

    log.info("cleanup_duplicate_strms: scanned=%d cleaned=%d skipped=%d", scanned, cleaned, skipped)
    return {"scanned": scanned, "cleaned": cleaned, "skipped": skipped}


def _self_heal_sample(sample_size: int = 10) -> None:
    """HEAD-check a random sample of existing .strm files. If a high fraction
    fail, log a warning and let the next cleanup cycle do the heavy lifting."""
    import random
    media = Path(MEDIA_PATH)
    if not media.is_dir():
        return
    strms: list[Path] = []
    for sub in ("movies", "series"):
        d = media / sub
        if d.is_dir():
            strms.extend(d.rglob("*.strm"))
    if len(strms) <= 5:
        return
    sample = random.sample(strms, min(sample_size, len(strms)))
    bad = 0
    for s in sample:
        try:
            url = s.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        # Catbox proxy URLs always work  -  skip the probe
        if "/stream/" in url and url.startswith("http://"):
            continue
        try:
            r = req_lib.head(url, timeout=5, allow_redirects=True)
            if r.status_code >= 400:
                bad += 1
        except Exception:
            bad += 1
    if bad and bad / len(sample) >= 0.3:
        log.warning("Self-heal probe: %d/%d sampled strms failed; cleanup will repair",
                    bad, len(sample))
