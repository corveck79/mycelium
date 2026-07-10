"""
MP4 fast-start proxy for Mycelium.

CDN-served MP4 files have moov at the END (mdat-before-moov).
This makes Plex/FFmpeg seek 15GB before knowing the codec.

Solution: fetch ftyp (32 bytes) + moov (~15MB) from the CDN once,
rewrite chunk offsets (stco/co64) so moov appears first, cache on disk.

Virtual fast-start layout:
  [ftyp][moov_rewritten][mdat_content...]

Original CDN layout:
  [ftyp][mdat_content...][moov]

Offset mapping (virtual → CDN):
  [0, ftyp_size)                    → CDN [0, ftyp_size)  (ftyp unchanged)
  [ftyp_size, ftyp_size+moov_size)  → serve from cached rewritten moov
  [ftyp_size+moov_size, ...)        → CDN [virtual - moov_size, ...)

stco/co64 delta: +moov_size  (mdat shifted right by moov_size in virtual file)
"""
from __future__ import annotations

import logging
import struct
import threading
import time
from pathlib import Path

import requests as req_lib

log = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 10
_READ_TIMEOUT    = 60
_MAX_MOOV_BYTES  = 128 * 1024 * 1024  # refuse to buffer a moov bigger than this
_MAX_FTYP_BYTES  = 1024 * 1024        # real ftyp boxes are a few dozen bytes; same cap idea as moov
_MAX_429_RETRIES = 4
_RETRY_BASE_DELAY_S = 0.3
# gunicorn runs with a small, fixed thread count for the whole app (see
# Dockerfile) -- serve_bytes() calls _get() inline on a live request thread
# while streaming a response, unlike build_and_cache()'s backgrounded calls,
# so it gets a much smaller retry budget (worst case ~0.9s vs ~4.5s) to
# bound how long a sustained CDN rate limit can hold one of those threads.
_LIVE_REQUEST_MAX_429_RETRIES = 2
_CACHE_DIR: Path | None = None # set by init()
# Per-token locks instead of one global lock, so building the fast-start
# cache for one movie doesn't block every other concurrent cold-start build.
# Not swept/evicted - same accepted tradeoff as catbox.py's _token_locks
# (unbounded but slow growth vs. the risk of deleting a lock while it's
# being handed out to another caller).
_cache_locks: dict[str, threading.Lock] = {}
_cache_locks_registry_lock = threading.Lock()


def _token_lock(token: str) -> threading.Lock:
    with _cache_locks_registry_lock:
        lock = _cache_locks.get(token)
        if lock is None:
            lock = threading.Lock()
            _cache_locks[token] = lock
        return lock


def init(cache_dir: str | Path) -> None:
    global _CACHE_DIR
    _CACHE_DIR = Path(cache_dir)
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_path(token: str) -> Path:
    assert _CACHE_DIR is not None, "mp4_faststart.init() not called"
    return _CACHE_DIR / f"{token}.fsh"


# ── Box parsing ───────────────────────────────────────────────────────────────

def _box_header(data: bytes | bytearray, pos: int) -> tuple[bytes, int, int]:
    """Return (box_type, box_size, header_size) at pos, or raise ValueError."""
    if pos + 8 > len(data):
        raise ValueError("truncated box header")
    size = struct.unpack_from(">I", data, pos)[0]
    typ  = bytes(data[pos + 4 : pos + 8])
    if size == 1:
        if pos + 16 > len(data):
            raise ValueError("truncated extended box header")
        size   = struct.unpack_from(">Q", data, pos + 8)[0]
        hdr    = 16
    elif size == 0:
        size   = len(data) - pos
        hdr    = 8
    else:
        hdr    = 8
    return typ, size, hdr


def _rewrite_offsets(moov: bytearray, delta: int, moov_offset: int) -> None:
    """Add delta to every stco/co64 chunk offset inside moov that points into
    mdat1 (the data before moov in the CDN file), in-place.

    Offsets >= moov_offset point into a second mdat block that comes AFTER
    moov in the CDN layout ([ftyp][mdat1][moov][mdat2]) - that region doesn't
    move in the virtual fast-start layout, so those offsets must stay
    untouched. Raises ValueError if a 32-bit stco offset would overflow.

    Only the classic (non-fragmented) moov container hierarchy is handled.
    moof/traf (fragmented MP4) use tfhd/trun, not stco/co64, and can't appear
    nested inside moov anyway - deliberately excluded so a fragmented file
    falls through as untouched/no-op rather than being mis-rewritten."""
    _CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts"}

    def _walk(start: int, end: int) -> None:
        pos = start
        while pos < end - 8:
            try:
                typ, size, hdr = _box_header(moov, pos)
            except ValueError:
                break
            if size < 8:
                break
            box_end = pos + size

            if typ in _CONTAINERS:
                _walk(pos + hdr, box_end)
            elif typ == b"stco":
                n = struct.unpack_from(">I", moov, pos + 12)[0]
                for i in range(n):
                    p = pos + 16 + i * 4
                    old = struct.unpack_from(">I", moov, p)[0]
                    if old >= moov_offset:
                        continue  # points into mdat2, unchanged in the virtual layout
                    new = old + delta
                    if new > 0xFFFFFFFF:
                        raise ValueError(
                            f"stco offset overflow: {old}+{delta} exceeds 32-bit range - "
                            "file needs co64, fast-start unsupported"
                        )
                    struct.pack_into(">I", moov, p, new)
            elif typ == b"co64":
                n = struct.unpack_from(">I", moov, pos + 12)[0]
                for i in range(n):
                    p = pos + 16 + i * 8
                    old = struct.unpack_from(">Q", moov, p)[0]
                    if old >= moov_offset:
                        continue
                    struct.pack_into(">Q", moov, p, old + delta)
            pos = box_end

    _walk(0, len(moov))


def _find_box_in(data: bytes, typ: bytes) -> int:
    """Return offset of first top-level box with the given type, or -1."""
    pos = 0
    while pos + 8 <= len(data):
        try:
            t, size, _ = _box_header(data, pos)
        except ValueError:
            break
        if t == typ:
            return pos
        pos += size
    return -1


# ── Fetch + cache ─────────────────────────────────────────────────────────────

def _get(url: str, start: int, end: int, max_retries: int = _MAX_429_RETRIES) -> bytes:
    headers = {"Range": f"bytes={start}-{end}"}
    attempt = 0
    while True:
        resp = req_lib.get(
            url,
            headers=headers,
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            stream=True,
        )
        if resp.status_code == 429 and attempt < max_retries:
            # A caller (spore-nfs/spore-smb) that hit this via the streaming
            # /spore-stream proxy has no chance to retry itself: Flask has
            # already committed the response status/Content-Length before
            # this generator runs, so a truncated body here means a
            # short-but-"200"-looking response reaches the client, which
            # every caller since had to treat as a hard failure. Retry
            # inline instead so a transient CDN rate limit doesn't turn
            # into a truncated stream.
            #
            # max_retries is caller-tunable because not every caller can
            # afford the full backoff budget: build_and_cache()'s calls run
            # in a background thread and can wait it out, but serve_bytes()
            # runs inline on a live gunicorn request thread (of which there
            # are only a handful total) while streaming a response, so it
            # passes a smaller budget to bound how long it can block one.
            backoff = _RETRY_BASE_DELAY_S * (2 ** attempt)
            log.warning(
                "CDN rate limited (429) for bytes=%d-%d, retrying in %.1fs (attempt %d/%d)",
                start, end, backoff, attempt + 1, max_retries,
            )
            time.sleep(backoff)
            attempt += 1
            continue
        if resp.status_code == 200:
            # CDN ignored the Range header and is about to send the WHOLE file
            # starting at byte 0, not at `start` - every offset computed from
            # this response would be wrong. Bail out instead of silently
            # buffering/misreading it (every real TorBox CDN response uses 206).
            raise ValueError(
                f"CDN returned HTTP 200 (ignored Range header) for bytes={start}-{end} - "
                "refusing to treat body as if it started at the requested offset"
            )
        if resp.status_code != 206:
            raise ValueError(f"CDN returned HTTP {resp.status_code} for bytes={start}-{end}")
        data = bytearray()
        for chunk in resp.iter_content(1 << 17):
            data += chunk
        return bytes(data)


def _locate_moov(cdn_url: str, cdn_size: int) -> tuple[int, int] | None:
    """
    Scan top-level box headers to find moov offset and size.
    Reads only 16 bytes per box header, so it's cheap even for 17 GB files.
    Returns (moov_offset, moov_size) or None.
    """
    pos = 0
    while pos < cdn_size - 8:
        raw = _get(cdn_url, pos, pos + 15)
        if len(raw) < 8:
            break
        try:
            typ, size, _ = _box_header(raw, 0)
        except ValueError:
            break
        if typ == b"moov":
            return pos, size
        if size < 8 or size > cdn_size - pos:
            # A box smaller than the minimum header (or one claiming to run
            # past EOF) means the file is malformed - bail out instead of
            # crawling forward one byte at a time, one HTTP request per box.
            break
        pos += size
    return None


def build_and_cache(cdn_url: str, token: str) -> bool:
    """
    Fetch ftyp + moov from CDN, build fast-start header, write to .fsh cache.
    Scans box headers sequentially so moov is found regardless of its position.
    Returns True on success.
    """
    path = _cache_path(token)
    with _token_lock(token):
        if path.exists():
            return True

        try:
            head = req_lib.head(cdn_url, timeout=_CONNECT_TIMEOUT, allow_redirects=True)
            cdn_size = int(head.headers["Content-Length"])

            def _atomic_write(dest: Path, data: bytes) -> None:
                tmp = dest.with_suffix(".tmp")
                tmp.write_bytes(data)
                tmp.replace(dest)

            # ftyp: first box  -  read header to get actual size, then fetch full box.
            # An oversized "size" here isn't necessarily a malformed MP4 -- it's
            # the expected result of parsing a non-MP4 container's first bytes
            # as an MP4 box header. MKV's EBML magic (0x1A45DFA3 = 440786851)
            # parses this way every time, so this must fall through to the same
            # "not an MP4" redirect-sentinel path _locate_moov() uses below,
            # not bail out -- bailing out here left build_and_cache() failing
            # forever for these tokens (no .fsh ever written -> every request
            # stuck on the slow cold-proxy path indefinitely).
            ftyp_hdr = _get(cdn_url, 0, 15)
            _, ftyp_size, _ = _box_header(ftyp_hdr, 0)
            if ftyp_size > _MAX_FTYP_BYTES:
                log.info(
                    "FastStart: ftyp size %d for token=%s exceeds cap %d "
                    "(likely non-MP4, e.g. MKV's EBML magic) - redirect sentinel",
                    ftyp_size, token, _MAX_FTYP_BYTES,
                )
                meta = struct.pack(">QQQQ", 0, 0, cdn_size, 0)
                _atomic_write(path, meta)
                return True
            ftyp_raw = _get(cdn_url, 0, ftyp_size - 1)
            ftyp = ftyp_raw[:ftyp_size]

            # Locate moov by scanning box headers
            result = _locate_moov(cdn_url, cdn_size)

            if result is None:
                # Not an MP4 (likely MKV): write redirect sentinel so spore-stream
                # issues a 302 to CDN directly. FFmpeg reads MKV from byte 0, no seeking.
                meta = struct.pack(">QQQQ", 0, 0, cdn_size, 0)
                _atomic_write(path, meta)
                log.info("FastStart: non-MP4 CDN for token=%s, stored redirect sentinel", token)
                return True

            moov_offset, moov_size = result

            if moov_size > _MAX_MOOV_BYTES:
                log.warning(
                    "FastStart: moov size %d for token=%s exceeds cap %d - "
                    "refusing to buffer (malformed/hostile CDN response?)",
                    moov_size, token, _MAX_MOOV_BYTES,
                )
                return False

            if moov_offset == ftyp_size:
                # Already fast-start: sentinel with moov_size=0 signals direct CDN redirect
                meta = struct.pack(">QQQQ", ftyp_size, 0, cdn_size, moov_offset)
                _atomic_write(path, meta)
                log.info("FastStart: already fast-start for token=%s, stored sentinel", token)
                return True

            # Fetch and rewrite moov
            moov = bytearray(_get(cdn_url, moov_offset, moov_offset + moov_size - 1))

            # Chunk offsets delta = moov_size: mdat1 shifts right by moov_size in virtual layout
            _rewrite_offsets(moov, moov_size, moov_offset)

            header = ftyp + bytes(moov)

            # .fsh: [8B ftyp_size][8B moov_size][8B cdn_size][8B moov_offset][header...]
            meta = struct.pack(">QQQQ", ftyp_size, moov_size, cdn_size, moov_offset)
            _atomic_write(path, meta + header)

            log.info(
                "FastStart: cached token=%s ftyp=%d moov=%d moov_offset=%d cdn_size=%d",
                token, ftyp_size, moov_size, moov_offset, cdn_size,
            )
            return True

        except Exception as exc:
            log.warning("FastStart: build failed for %s: %s", token, exc)
            return False


def load(token: str) -> dict | None:
    """
    Load cached fast-start info for token.
    Returns dict with keys: ftyp_size, moov_size, cdn_size, header (bytes)
    or None if not cached.
    """
    path = _cache_path(token)
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
        if len(raw) < 32:
            # Legacy .fsh without moov_offset field (3-field header)
            ftyp_size, moov_size, cdn_size = struct.unpack_from(">QQQ", raw, 0)
            moov_offset = ftyp_size if moov_size == 0 else cdn_size - moov_size
            header = raw[24:]
        else:
            ftyp_size, moov_size, cdn_size, moov_offset = struct.unpack_from(">QQQQ", raw, 0)
            header = raw[32:]
        return {
            "ftyp_size":    ftyp_size,
            "moov_size":    moov_size,
            "moov_offset":  moov_offset,
            "cdn_size":     cdn_size,
            "header":       header,
            "header_size":  len(header),
            "already_fast": moov_size == 0,
        }
    except Exception as exc:
        log.warning("FastStart: load failed for %s: %s", token, exc)
        return None


def extract_codec_private(token: str) -> bytes | None:
    """Extract HEVC (hvcC) or AVC (avcC) decoder config bytes from the cached moov.
    Returns the box payload (without 8-byte header), or None if not found."""
    info = load(token)
    if not info:
        return None
    moov = info["header"][info["ftyp_size"]:]
    for tag in (b"hvcC", b"avcC"):
        i = moov.find(tag)
        if i >= 4:
            sz = struct.unpack_from(">I", moov, i - 4)[0]
            if 8 <= sz <= len(moov) - (i - 4):
                return bytes(moov[i + 4 : i - 4 + sz])
    return None


# ── Virtual offset mapping ────────────────────────────────────────────────────

def virtual_to_cdn(virtual_offset: int, info: dict) -> int | None:
    """
    Map a virtual fast-start file offset to the real CDN offset.
    Returns None if the offset is inside the cached header (no CDN fetch needed).
    """
    if virtual_offset < info["header_size"]:
        return None  # served from cached header
    return virtual_offset - info["moov_size"]


def serve_bytes(info: dict, cdn_url: str, v_start: int, v_end: int) -> bytes:
    """
    Return bytes [v_start, v_end] from the virtual fast-start file.

    Virtual layout: [ftyp][moov_rewritten][mdat1][mdat2]
    CDN layout:     [ftyp][mdat1][moov][mdat2]

    Offset mapping for CDN data regions:
      mdat1: virtual [hdr_size, moov_offset+moov_size) → CDN [ftyp_size, moov_offset)
             i.e. cdn = virtual - moov_size
      mdat2: virtual [moov_offset+moov_size, cdn_size) → CDN [moov_offset+moov_size, cdn_size)
             i.e. cdn = virtual  (unchanged)
    """
    header      = info["header"]
    hdr_size    = info["header_size"]
    moov_size   = info["moov_size"]
    moov_offset = info["moov_offset"]
    mdat2_start = moov_offset + moov_size  # virtual == CDN for mdat2

    out = bytearray()
    pos = v_start

    # Region 1: cached header (ftyp + rewritten moov)
    if pos < hdr_size:
        chunk_end = min(v_end, hdr_size - 1)
        out += header[pos : chunk_end + 1]
        pos = chunk_end + 1

    # Region 2: mdat1 (before moov in CDN) — cdn = virtual - moov_size
    if pos <= v_end and pos < mdat2_start:
        chunk_end = min(v_end, mdat2_start - 1)
        out += _get(cdn_url, pos - moov_size, chunk_end - moov_size,
                     max_retries=_LIVE_REQUEST_MAX_429_RETRIES)
        pos = chunk_end + 1

    # Region 3: mdat2 (after moov in CDN) — cdn = virtual
    if pos <= v_end:
        out += _get(cdn_url, pos, v_end, max_retries=_LIVE_REQUEST_MAX_429_RETRIES)

    return bytes(out)
