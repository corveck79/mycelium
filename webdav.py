"""Read-only WebDAV server exposing the .strm library as virtual .mkv files.

Designed for mounting via davfs2 on a Synology DSM host so Plex / Emby
containers can scan the library as if it were a real filesystem.

Path mapping
------------
WebDAV  /dav/movies/Inception (2010)/Inception (2010).mkv
disk    MEDIA_PATH/movies/Inception (2010)/Inception (2010).strm

The .strm file's content is the upstream URL (TorBox CDN, Catbox proxy, or
RealDebrid direct link). For each GET we either pass it through or resolve
the Catbox proxy to a fresh CDN URL via catbox.materialize().

Supported methods: OPTIONS, PROPFIND, HEAD, GET (with Range).
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone
from email.utils import formatdate
from pathlib import Path
from urllib.parse import quote, unquote
from xml.sax.saxutils import escape as xml_escape

import cachetools
import requests
from flask import Response, request

import settings
from config import WEBDAV_URL_CACHE_TTL_SECONDS

log = logging.getLogger(__name__)

_VIDEO_MIME = {
    ".mkv": "video/x-matroska",
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".ts":  "video/mp2t",
    ".m2ts": "video/mp2t",
}

# (resolved_url, expires_at) keyed by absolute on-disk .strm path. Bounded so
# a Plex / Emby scanner walking a large library cannot grow it indefinitely.
# Per-entry expires_at still drives the WebDAV freshness check; the cachetools
# TTL just guarantees inactive entries get evicted.
_url_cache: "cachetools.TTLCache[Path, tuple[str, datetime]]" = cachetools.TTLCache(
    maxsize=20000, ttl=max(WEBDAV_URL_CACHE_TTL_SECONDS, 3600) + 600,
)
# upstream content-length, keyed by .strm path
_size_cache: "cachetools.TTLCache[Path, tuple[int, datetime]]" = cachetools.TTLCache(
    maxsize=20000, ttl=86400,
)
_cache_lock = threading.Lock()

_CATBOX_TOKEN_RE = re.compile(r"/stream/([a-fA-F0-9]{8,})$")


# ─────────────────────────────────────────────────────────────────────────────
# Path translation
# ─────────────────────────────────────────────────────────────────────────────

def _media_root() -> Path:
    return Path(settings.get("MEDIA_PATH", "/data/media"))


def _prefix() -> str:
    p = settings.get("WEBDAV_PATH_PREFIX", "/dav")
    return p if p.startswith("/") else f"/{p}"


def _webdav_to_disk(webdav_path: str) -> Path | None:
    """Map a WebDAV URL path to a disk path under MEDIA_PATH.
    Files: .mkv → .strm. Dirs are returned as-is. Returns None on traversal."""
    prefix = _prefix().rstrip("/")
    if not webdav_path.startswith(prefix):
        return None
    rel = unquote(webdav_path[len(prefix):]).lstrip("/")
    media = _media_root().resolve()
    disk = (media / rel).resolve() if rel else media
    # Path traversal guard
    try:
        disk.relative_to(media)
    except ValueError:
        return None
    if disk.suffix.lower() in _VIDEO_MIME and not disk.exists():
        # .mkv requested → look for .strm sibling
        strm = disk.with_suffix(".strm")
        if strm.exists():
            return strm
    return disk


def _disk_to_webdav(disk_path: Path, is_collection: bool = False) -> str:
    """Map a disk path to a WebDAV URL path (.strm → .mkv)."""
    media = _media_root().resolve()
    rel = disk_path.resolve().relative_to(media)
    parts = list(rel.parts)
    if parts and parts[-1].endswith(".strm"):
        parts[-1] = parts[-1][:-5] + ".mkv"
    href = _prefix().rstrip("/") + "/" + "/".join(quote(p) for p in parts)
    if is_collection and not href.endswith("/"):
        href += "/"
    return href


# ─────────────────────────────────────────────────────────────────────────────
# Upstream resolution + size lookup
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_upstream(strm_path: Path) -> str | None:
    """Read the .strm, resolve any /stream/<token> internally, return fresh URL."""
    ttl = settings.get("WEBDAV_URL_CACHE_TTL_SECONDS", 3600)
    now = datetime.now(timezone.utc)
    with _cache_lock:
        cached = _url_cache.get(strm_path)
        if cached and cached[1] > now:
            return cached[0]

    try:
        raw = strm_path.read_text(encoding="utf-8").strip()
    except Exception as exc:
        log.debug("WebDAV: read %s failed: %s", strm_path, exc)
        return None

    upstream = raw
    m = _CATBOX_TOKEN_RE.search(raw)
    if m:
        try:
            import catbox
            resolved = catbox.materialize(m.group(1))
        except Exception as exc:
            log.warning("WebDAV: catbox.materialize failed: %s", exc)
            resolved = None
        if not resolved:
            return None
        upstream = resolved

    expires = datetime.fromtimestamp(now.timestamp() + ttl, tz=timezone.utc)
    with _cache_lock:
        _url_cache[strm_path] = (upstream, expires)
    return upstream


def _content_length(strm_path: Path) -> int:
    """HEAD the upstream URL to learn the file size. Cached aggressively."""
    ttl = settings.get("WEBDAV_URL_CACHE_TTL_SECONDS", 3600)
    now = datetime.now(timezone.utc)
    with _cache_lock:
        cached = _size_cache.get(strm_path)
        if cached and cached[1] > now:
            return cached[0]
    upstream = _resolve_upstream(strm_path)
    if not upstream:
        return 0
    try:
        r = requests.head(upstream, timeout=10, allow_redirects=True)
        size = int(r.headers.get("Content-Length") or 0)
    except Exception as exc:
        log.debug("WebDAV: HEAD %s failed: %s", upstream, exc)
        size = 0
    if size:
        with _cache_lock:
            _size_cache[strm_path] = (
                size,
                datetime.fromtimestamp(now.timestamp() + ttl, tz=timezone.utc),
            )
    return size


def _mime_for(disk_path: Path) -> str:
    return _VIDEO_MIME.get(disk_path.suffix.lower(), "application/octet-stream")


# ─────────────────────────────────────────────────────────────────────────────
# PROPFIND helpers
# ─────────────────────────────────────────────────────────────────────────────

def _http_date(ts: float) -> str:
    return formatdate(ts, usegmt=True)


def _entry_xml(href: str, is_collection: bool, size: int, mtime: float,
                mime: str = "") -> str:
    last_mod = _http_date(mtime)
    if is_collection:
        resourcetype = "<D:resourcetype><D:collection/></D:resourcetype>"
        props = f"""
            {resourcetype}
            <D:getlastmodified>{last_mod}</D:getlastmodified>
            <D:displayname>{xml_escape(href.rsplit('/', 2)[-2] if href.endswith('/') else href.rsplit('/', 1)[-1])}</D:displayname>
        """
    else:
        props = f"""
            <D:resourcetype/>
            <D:getlastmodified>{last_mod}</D:getlastmodified>
            <D:getcontentlength>{size}</D:getcontentlength>
            <D:getcontenttype>{xml_escape(mime)}</D:getcontenttype>
            <D:displayname>{xml_escape(href.rsplit('/', 1)[-1])}</D:displayname>
        """
    return f"""<D:response>
        <D:href>{href}</D:href>
        <D:propstat>
          <D:prop>{props.strip()}</D:prop>
          <D:status>HTTP/1.1 200 OK</D:status>
        </D:propstat>
      </D:response>"""


def _propfind_xml(entries: list[str]) -> str:
    body = "".join(entries)
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<D:multistatus xmlns:D="DAV:">' + body + "</D:multistatus>"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Method handlers
# ─────────────────────────────────────────────────────────────────────────────

def _options() -> Response:
    return Response(
        "",
        status=200,
        headers={
            "DAV": "1, 2",
            "MS-Author-Via": "DAV",
            "Allow": "OPTIONS, GET, HEAD, PROPFIND",
            "Public": "OPTIONS, GET, HEAD, PROPFIND",
            "Content-Length": "0",
        },
    )


def _propfind(disk: Path, depth: str) -> Response:
    if not disk.exists():
        return Response("Not Found", status=404)

    entries: list[str] = []
    # Always include the resource itself
    if disk.is_dir():
        href = _disk_to_webdav(disk, is_collection=True)
        entries.append(_entry_xml(href, True, 0, disk.stat().st_mtime))
        if depth in ("1", "infinity"):
            for child in sorted(disk.iterdir()):
                if child.is_dir():
                    entries.append(_entry_xml(
                        _disk_to_webdav(child, is_collection=True),
                        True, 0, child.stat().st_mtime,
                    ))
                elif child.suffix == ".strm":
                    entries.append(_entry_xml(
                        _disk_to_webdav(child),
                        False,
                        _content_length(child),
                        child.stat().st_mtime,
                        _VIDEO_MIME[".mkv"],
                    ))
    else:
        if disk.suffix != ".strm":
            return Response("Not Found", status=404)
        entries.append(_entry_xml(
            _disk_to_webdav(disk),
            False,
            _content_length(disk),
            disk.stat().st_mtime,
            _VIDEO_MIME[".mkv"],
        ))

    body = _propfind_xml(entries)
    return Response(body, status=207, mimetype="application/xml; charset=utf-8")


def _head(disk: Path) -> Response:
    if not disk.exists() or disk.suffix != ".strm":
        return Response("Not Found", status=404)
    size = _content_length(disk)
    return Response(
        "",
        status=200,
        headers={
            "Content-Length": str(size),
            "Content-Type": _VIDEO_MIME[".mkv"],
            "Last-Modified": _http_date(disk.stat().st_mtime),
            "Accept-Ranges": "bytes",
        },
    )


def _get(disk: Path) -> Response:
    if not disk.exists() or disk.suffix != ".strm":
        return Response("Not Found", status=404)
    upstream = _resolve_upstream(disk)
    if not upstream:
        return Response("Upstream unavailable", status=502)

    range_header = request.headers.get("Range")
    fwd_headers = {"Range": range_header} if range_header else {}
    try:
        r = requests.get(upstream, headers=fwd_headers, stream=True,
                          timeout=30, allow_redirects=True)
    except Exception as exc:
        log.warning("WebDAV: upstream GET failed: %s", exc)
        return Response("Upstream error", status=502)

    if r.status_code >= 400:
        # Stale URL  -  drop the cache so the next read re-resolves
        with _cache_lock:
            _url_cache.pop(disk, None)
        return Response(f"Upstream {r.status_code}", status=r.status_code)

    def stream():
        try:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk
        except Exception as exc:
            log.debug("WebDAV: stream interrupted: %s", exc)
        finally:
            r.close()

    resp_headers = {
        "Content-Type": _VIDEO_MIME[".mkv"],
        "Accept-Ranges": "bytes",
        "Last-Modified": _http_date(disk.stat().st_mtime),
    }
    if r.headers.get("Content-Length"):
        resp_headers["Content-Length"] = r.headers["Content-Length"]
    if r.headers.get("Content-Range"):
        resp_headers["Content-Range"] = r.headers["Content-Range"]
    return Response(stream(), status=r.status_code, headers=resp_headers)


# ─────────────────────────────────────────────────────────────────────────────
# Public entrypoint (called by Flask route)
# ─────────────────────────────────────────────────────────────────────────────

def dispatch(path_suffix: str) -> Response:
    """Handle a WebDAV request. path_suffix is everything after WEBDAV_PATH_PREFIX."""
    if not settings.get("WEBDAV_ENABLED", False):
        return Response("WebDAV disabled", status=404)

    method = request.method.upper()
    full_path = _prefix().rstrip("/") + "/" + path_suffix
    if method == "OPTIONS":
        return _options()

    disk = _webdav_to_disk(full_path)
    if disk is None:
        return Response("Bad path", status=400)

    if method == "PROPFIND":
        depth = request.headers.get("Depth", "1")
        return _propfind(disk, depth)
    if method == "HEAD":
        return _head(disk)
    if method == "GET":
        return _get(disk)

    return Response("Method not allowed", status=405,
                     headers={"Allow": "OPTIONS, GET, HEAD, PROPFIND"})


def invalidate_cache(path: Path | None = None) -> None:
    """Drop cached URL/size for a single .strm (or everything)."""
    with _cache_lock:
        if path is None:
            _url_cache.clear()
            _size_cache.clear()
        else:
            _url_cache.pop(path, None)
            _size_cache.pop(path, None)
