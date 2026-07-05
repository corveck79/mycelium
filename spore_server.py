"""
Mycelium Spore server.

Listens on a TCP port and serves byte ranges to the Plex interceptor (.so).

Protocol (one connection per request):
  Request:  "<token> <offset> <count>\\n"
  Response: "OK <actual_count>\\n<bytes...>"
            "ERR <message>\\n"

The server resolves the CDN URL for a token via catbox.materialize() and
proxies the requested byte range directly from the TorBox CDN.
"""
from __future__ import annotations

import logging
import os
import socket
import threading

import requests as req_lib

log = logging.getLogger(__name__)

_DEFAULT_HOST = "127.0.0.1"  # no auth on this protocol - never expose beyond loopback by default
_DEFAULT_PORT = 8089
_MAX_COUNT    = 10 * 1024 * 1024   # cap per request at 10 MB
_CONNECT_TIMEOUT = 10
_READ_TIMEOUT    = 60


def _get_cdn_url(token: str, allow_readd: bool = False) -> str | None:
    """Resolve CDN URL for a token.

    Fast path: in-memory URL cache (no network calls).
    Slow path: TorBox library check via catbox.materialize().
      allow_readd=False during Plex library scans to avoid mass-adding torrents.
      allow_readd=True only when a cached URL has expired at the CDN (HTTP 4xx).
    """
    try:
        import catbox
        url = catbox._cache_get(token)
        if url:
            return url
        return catbox.materialize(token, allow_readd=allow_readd)
    except Exception as exc:
        log.warning("Spore: CDN URL lookup failed for %s: %s", token, exc)
        return None


def _fetch_range(cdn_url: str, offset: int, count: int) -> bytes | None:
    """Fetch a byte range from a CDN URL via HTTP Range request."""
    end = offset + count - 1
    headers = {"Range": f"bytes={offset}-{end}"}
    try:
        resp = req_lib.get(
            cdn_url,
            headers=headers,
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            stream=True,
        )
        if resp.status_code == 200 and offset != 0:
            # CDN ignored the Range header and is sending the file from byte
            # 0 - the first `count` bytes of that body are NOT bytes
            # [offset, offset+count), so returning them would silently serve
            # wrong data. A 200 is only actually correct here when offset==0.
            log.warning(
                "Spore: CDN returned HTTP 200 (ignored Range) for %s bytes=%d-%d",
                cdn_url[:60], offset, end,
            )
            return None
        if resp.status_code not in (200, 206):
            log.warning(
                "Spore: CDN returned HTTP %d for %s bytes=%d-%d",
                resp.status_code, cdn_url[:60], offset, end,
            )
            return None
        data = bytearray()
        for chunk in resp.iter_content(65536):
            data += chunk
            if len(data) >= count:
                break
        return bytes(data[:count])
    except Exception as exc:
        log.warning("Spore: range fetch failed (%s bytes=%d-%d): %s",
                    cdn_url[:60], offset, end, exc)
        return None


def _handle(conn: socket.socket, addr) -> None:
    """Handle one Spore client connection."""
    log.info("Spore: connection from %s", addr)
    try:
        # Read request line
        buf = b""
        while not buf.endswith(b"\n"):
            c = conn.recv(1)
            if not c:
                return
            buf += c
            if len(buf) > 256:
                conn.sendall(b"ERR line too long\n")
                return

        parts = buf.decode().strip().split()
        if len(parts) != 3:
            conn.sendall(b"ERR bad request\n")
            return

        token, offset_s, count_s = parts
        try:
            offset = int(offset_s)
            count  = min(int(count_s), _MAX_COUNT)
        except ValueError:
            conn.sendall(b"ERR bad numbers\n")
            return

        log.info("Spore: request token=%s offset=%d count=%d", token, offset, count)
        cdn_url = _get_cdn_url(token, allow_readd=False)
        if not cdn_url:
            conn.sendall(b"ERR no cdn url\n")
            return

        # Use fast-start virtual layout if cached (moov-first, correct offsets).
        try:
            import mp4_faststart
            fsh = mp4_faststart.load(token)
        except Exception:
            fsh = None

        if fsh:
            try:
                data = mp4_faststart.serve_bytes(fsh, cdn_url, offset, offset + count - 1)
            except Exception as exc:
                log.warning("Spore: fast-start serve failed: %s", exc)
                data = None
        else:
            data = _fetch_range(cdn_url, offset, count)

        if data is None:
            # CDN URL may have expired - invalidate cache and get a fresh one
            try:
                import catbox
                catbox.invalidate_url_cache(token)
                log.info("Spore: CDN URL expired for %s, refreshing", token)
            except Exception:
                pass
            cdn_url = _get_cdn_url(token, allow_readd=True)
            if cdn_url:
                data = _fetch_range(cdn_url, offset, count)
        if data is None:
            conn.sendall(b"ERR fetch failed\n")
            return

        conn.sendall(f"OK {len(data)}\n".encode())
        conn.sendall(data)

    except Exception as exc:
        log.warning("Spore: handler error: %s", exc)
        try:
            conn.sendall(b"ERR internal\n")
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _serve(srv: socket.socket) -> None:
    import time
    while True:
        try:
            conn, addr = srv.accept()
            t = threading.Thread(
                target=_handle,
                args=(conn, addr),
                daemon=True,
                name=f"spore-conn-{addr}",
            )
            t.start()
        except OSError:
            # Socket closed - normal shutdown
            break
        except Exception as exc:
            log.warning("Spore: accept error: %s", exc)
            time.sleep(1)


def start(host: str = _DEFAULT_HOST,
          port: int = _DEFAULT_PORT) -> socket.socket:
    """Start the Spore TCP server in a background daemon thread.
    Returns the server socket (for shutdown if needed)."""
    env_port = os.environ.get("MYCELIUM_SPORE_PORT")
    if env_port:
        try:
            port = int(env_port)
        except ValueError:
            pass
    host = os.environ.get("MYCELIUM_SPORE_HOST", host)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(64)

    log.info("Mycelium Spore server listening on %s:%d", host, port)

    t = threading.Thread(
        target=_serve,
        args=(srv,),
        daemon=True,
        name="spore-server",
    )
    t.start()
    return srv
