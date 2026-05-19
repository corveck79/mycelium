import logging
import re
from pathlib import Path

import requests as req_lib

import jellyfin
import torbox as torbox_mod
from config import CATBOX_MODE, MEDIA_PATH, TORBOX_API_KEY, TORBOX_BASE_URL

log = logging.getLogger(__name__)

_VIDEO_EXTS = {'.mkv', '.mp4', '.avi', '.m4v', '.mov', '.wmv', '.flv', '.ts', '.m2ts', '.webm'}

_EP_RE = re.compile(r'[Ss](\d{1,2})[Ee](\d{1,2})', re.IGNORECASE)
_YEAR_RE = re.compile(r'(?<!\d)((?:19|20)\d{2})(?!\d)')
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
    pool = non_trailer or videos
    big = [f for f in pool if (f.get('size') or 0) >= _MIN_MOVIE_SIZE]
    return max(big or pool, key=lambda f: f.get('size') or 0)


def _clean(s: str) -> str:
    return re.sub(r'[._]', ' ', s).strip()


def _strip_junk(s: str) -> str:
    return _JUNK_RE.sub('', s).strip(' .-_ ')


def _safe(s: str) -> str:
    return _SAFE_RE.sub('', s).strip()


def _parse_info(torrent_name: str, file_name: str) -> dict | None:
    """Extract title/year/season/episode from torrent and file names."""
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
        "token": TORBOX_API_KEY,
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


def _write_strm(path: Path, url: str) -> bool:
    """Write .strm file only if it doesn't exist. Returns True if a new file was written."""
    if path.exists():
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
    if CATBOX_MODE:
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
    torrent_name = item.get('name') or ''
    files = item.get('files') or []

    if not torbox_mod._is_ready(item):
        return 0

    is_series = bool(_EP_RE.search(_clean(torrent_name)) or
                     re.search(r'\bS\d{1,2}\b', torrent_name, re.IGNORECASE))
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


def create_strm_for_torrent(torrent_id: int, title: str, media_type: str) -> int:
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
        return 1 if _write_strm(path, url) else 0

    return process_torrent(item)


def run_once() -> int:
    """Scan entire TorBox mylist and create any missing .strm files. Returns new file count."""
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
    if run_once() > 0:
        jellyfin.refresh_library()
