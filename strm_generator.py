import logging
import re
from pathlib import Path

import requests as req_lib

import db
import jellyfin
import settings
import torbox as torbox_mod
from config import MEDIA_PATH, TORBOX_BASE_URL

log = logging.getLogger(__name__)

_VIDEO_EXTS = {'.mkv', '.mp4', '.avi', '.m4v', '.mov', '.wmv', '.flv', '.ts', '.m2ts', '.webm'}

_EP_RE = re.compile(r'[Ss](\d{1,2})[Ee](\d{1,2})', re.IGNORECASE)
_YEAR_RE = re.compile(r'(?<!\d)((?:19|20)\d{2})(?!\d)')
# Strip leading site/group prefixes from torrent names before parsing:
#   [DEVIL-TORRENTS PL]  /  rutor.info  /  www.UIndex.org  /  HIDRATORRENTS.ORG  etc.
_SITE_PREFIX_RE = re.compile(
    r'^(?:\[[^\]]*\]\s*|(?:www\.|https?://)\S+\s*|'
    r'(?:rutor|hidratorrents|warmachine|xtorrenty|superseed|byethost\d*|'
    r'uindex|devil-torrents)\s*[\.\-]?\s*(?:info|org|pl|com|net)?\s*[-–—\s]*)+'
    , re.IGNORECASE,
)
# Strip leading Cyrillic block (keeps Latin title when torrent has both)
_CYRILLIC_PREFIX_RE = re.compile(r'^[Ѐ-ӿ\s\(\)\[\]\.,\-–—«»]+')
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
_MIN_MOVIE_SIZE = 200 * 1024 * 1024  # 200 MB — anything smaller is likely a trailer/sample


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


def _write_nfo(strm_path: Path, imdb_id: str | None, tmdb_id: int | None = None,
               media_type: str = "movie", nfo_path: Path | None = None) -> None:
    """Write a Kodi/Jellyfin NFO sidecar. nfo_path overrides the default
    (strm_path.with_suffix('.nfo')) so callers can write tvshow.nfo anywhere."""
    if not imdb_id and not tmdb_id:
        return
    nfo_path = nfo_path or strm_path.with_suffix(".nfo")
    if nfo_path.exists():
        return
    m = _YEAR_RE.search(strm_path.parent.name)
    year = int(m.group(1)) if m else None
    title = _YEAR_RE.sub("", strm_path.parent.name).strip() if m else strm_path.parent.name

    if media_type == "movie":
        year_tag = f"\n  <year>{year}</year>" if year else ""
        uid_tags = ""
        if imdb_id:
            uid_tags += f'  <uniqueid type="imdb" default="true">{imdb_id}</uniqueid>\n'
        if tmdb_id:
            uid_tags += f'  <uniqueid type="tmdb">{tmdb_id}</uniqueid>\n'
        content = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f"<movie>\n  <title>{title}</title>{year_tag}\n{uid_tags}</movie>\n"
        )
    else:
        uid_tags = ""
        if imdb_id:
            uid_tags += f'  <uniqueid type="imdb" default="true">{imdb_id}</uniqueid>\n'
        if tmdb_id:
            uid_tags += f'  <uniqueid type="tmdb">{tmdb_id}</uniqueid>\n'
        content = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f"<tvshow>\n  <title>{title}</title>\n{uid_tags}</tvshow>\n"
        )
    try:
        nfo_path.write_text(content, encoding="utf-8")
        log.info("Wrote NFO: %s", nfo_path)
    except Exception as exc:
        log.warning("Could not write NFO %s: %s", nfo_path, exc)


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


def create_lazy_movie_strm(info_hash: str, magnet: str, title: str,
                            year: int | None, imdb_id: str | None = None,
                            tmdb_id: int | None = None, quality: str | None = None,
                            source: str | None = None, size_gb: float | None = None) -> bool:
    """Write a Catbox virtual movie .strm WITHOUT adding the torrent to TorBox.
    createtorrent is deferred until first playback (see catbox.materialize).
    Atomically writes .nfo, poster.jpg, fanart.jpg, and requests subtitles.
    Returns True if a new .strm was written."""
    import catbox

    # imdb_id is leading: check if a folder already exists for this movie.
    if imdb_id:
        existing = _find_movie_folder_by_imdb(imdb_id)
        if existing:
            log.info("create_lazy_movie_strm: folder for %s already exists (%s) — skipping",
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
        if imdb_id or tmdb_id:
            _write_nfo(path, imdb_id, tmdb_id)
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
    return written


def create_lazy_episode_strm(info_hash: str, magnet: str, title: str,
                               season: int, episode: int,
                               imdb_id: str | None = None,
                               quality: str | None = None,
                               source: str | None = None,
                               size_gb: float | None = None) -> bool:
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
    if written and imdb_id:
        series_root = path.parent.parent
        tvshow_nfo = series_root / "tvshow.nfo"
        if not tvshow_nfo.exists():
            _write_nfo(path, imdb_id, nfo_path=tvshow_nfo, media_type="series")
        try:
            import nfo_generator
            nfo_generator.fetch_images_for_folder(series_root, imdb_id, "tv")
        except Exception as exc:
            log.debug("Image fetch skipped for %s: %s", safe_title, exc)
    return written


def _norm_title(s: str) -> str:
    """Normalize a folder/title string for fuzzy duplicate detection.
    Strips year in parens, leading articles, punctuation, and lowercases."""
    s = re.sub(r'\(\d{4}\)', '', s)           # remove (year)
    s = re.sub(r'^(the|a|an)\s+', '', s, flags=re.IGNORECASE)  # strip leading article
    return re.sub(r'[^a-z0-9]', '', s.lower())  # alphanumeric only


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
                    log.info("Skipping duplicate strm %s — already have %s", path.parent.name, existing.name)
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
    # skip entirely — avoids creating a second folder with the torrent-parsed title
    # when the movie was originally added with the TMDB title.
    if settings.get("CATBOX_MODE", False):
        info_hash = (item.get('hash') or '').lower()
        if info_hash:
            existing = db.get_virtual_item_by_hash(info_hash)
            if existing and existing.get('strm_path') and Path(existing['strm_path']).exists():
                log.debug("process_torrent: %s already has strm at %s — skipping",
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

    virtual_items is the source of truth — torrents are not in TorBox when
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
                log.debug("migrate: no canonical name for %s — skipping", imdb_id)
                skipped += len(folders)
                continue

            canonical_path = root / canonical

            if len(folders) == 1:
                folder = folders[0]
                if folder.resolve() == canonical_path.resolve():
                    skipped += 1
                    continue
                if canonical_path.exists():
                    log.warning("migrate: target '%s' already exists for %s — skipping",
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
                        # canonical already exists — it might be one of the others
                        if canonical_path in ordered:
                            keep = canonical_path
                        else:
                            log.warning("migrate: canonical '%s' exists but isn't in group — skipping",
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
    2. Direct TorBox CDN URL in .strm — expired after ~24h.
    3. Catbox proxy URL whose token is NOT in virtual_items DB — 404 on play.

    Repair strategy for each broken item:
      a. If a virtual_item exists for that imdb_id → write/rewrite the .strm
         to point at the correct catbox proxy URL.
      b. Otherwise → delete the broken .strm (if any) and requeue via processor
         so it gets a fresh catbox token on the next pass.
    Returns a summary dict with counts.
    """
    import re as _re
    import catbox as _catbox
    from config import CATBOX_HOST
    catbox_base = CATBOX_HOST.rstrip("/") + "/stream/"

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
            continue  # has at least one .strm — handled in pass 2
        # Skip if a sibling folder with the same normalised title already has a .strm.
        norm = _norm_title(movie_dir.name)
        if any(
            sib.is_dir() and sib != movie_dir
            and _norm_title(sib.name) == norm
            and any(sib.glob("*.strm"))
            for sib in root.iterdir()
        ):
            log.debug("repair_strms: skipping %s — duplicate of sibling with .strm", movie_dir.name)
            skipped += 1
            continue
        # No .strm — check if there's a .nfo we can use to requeue
        imdb_id = _nfo_imdb(movie_dir)
        if not imdb_id:
            log.debug("repair_strms: no .nfo imdb_id in %s — skipping", movie_dir.name)
            skipped += 1
            continue
        missing += 1
        expected_strm = movie_dir / f"{movie_dir.name}.strm"
        if _relink(imdb_id, expected_strm):
            relinked += 1
        else:
            log.info("repair_strms: no virtual_item for %s — requeuing", movie_dir.name)
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

        # Valid catbox proxy URL — verify token is in DB.
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
            log.warning("repair_strms: no imdb_id for %s — skipping", strm_path)
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
        # Catbox proxy URLs always work — skip the probe
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
