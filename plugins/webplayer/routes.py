import db
import auth
import catbox
import requests as req_lib
from flask import Blueprint, abort, jsonify, request, send_file, Response
from flask import session as flask_session
from . import web_player

bp = Blueprint("webplayer_routes", __name__)


def _check_enabled():
    rec = auth.current_user_record()
    if not rec or not rec.get("webplayer_enabled"):
        abort(403)
    return rec


@bp.post("/ui/api/web-player/prepare")
def web_player_prepare():
    _check_enabled()
    d = request.json or {}
    job_id = web_player.start_prepare_job(
        imdb_id    = d["imdb_id"],
        media_type = d["media_type"],
        season     = d.get("season"),
        episode    = d.get("episode"),
    )
    return jsonify(job_id=job_id)


@bp.get("/ui/api/web-player/status/<job_id>")
def web_player_status(job_id: str):
    job = web_player.get_job(job_id)
    if not job:
        abort(404)
    return jsonify(
        status      = job.status.value,
        message     = job.message,
        token       = job.token,
        stream_url  = job.stream_url,
        stream_type = job.stream_type,
        cdn_url     = job.cdn_url,
        file_info   = job.file_info,
        error       = job.error,
    )


@bp.get("/stream/<token>/hls/<path:filename>")
def stream_hls_file(token: str, filename: str):
    """Serve any HLS file for a session.

    Supports mpegts (.ts) and fragmented MP4 (.m4s + init.mp4) segments,
    plus all playlist variants (.m3u8).
    """
    if "/" in filename:
        abort(400)
    s = web_player.get_session(token)
    if not s:
        abort(404)
    p = s.tmp_dir / filename
    if not p.exists():
        abort(404)
    s.touch()
    if filename.endswith(".m3u8"):
        return send_file(p, mimetype="application/vnd.apple.mpegurl")
    if filename.endswith(".ts"):
        return send_file(p, mimetype="video/mp2t")
    if filename.endswith(".m4s"):
        return send_file(p, mimetype="video/iso.segment")
    if filename.endswith(".mp4"):
        return send_file(p, mimetype="video/mp4")
    abort(400)


@bp.get("/stream/<token>/direct")
def stream_direct(token: str):
    s = web_player.get_direct_session(token)
    if not s:
        abort(404)
    s.touch()

    cdn_url = catbox.materialize(token, allow_readd=True)
    if not cdn_url:
        abort(503)

    headers = {}
    range_hdr = request.headers.get("Range")
    if range_hdr:
        headers["Range"] = range_hdr

    upstream = req_lib.get(cdn_url, headers=headers, stream=True, timeout=30)

    response_headers = {
        "Content-Type":   s.content_type,
        "Accept-Ranges":  "bytes",
    }
    for h in ("Content-Length", "Content-Range"):
        if upstream.headers.get(h):
            response_headers[h] = upstream.headers[h]

    return Response(
        upstream.iter_content(chunk_size=65536),
        status=upstream.status_code,
        headers=response_headers,
    )


@bp.get("/stream/<token>/subtitles")
def stream_subtitles_list(token: str):
    return jsonify(subtitles=web_player.list_subtitles(token))


@bp.get("/stream/<token>/subtitles/<filename>")
def stream_subtitle_file(token: str, filename: str):
    if "/" in filename or not filename.endswith(".vtt"):
        abort(400)
    s = web_player.get_session(token)
    if not s:
        abort(404)
    p = s.tmp_dir / filename
    if not p.exists():
        abort(404)
    return send_file(p, mimetype="text/vtt")


@bp.post("/stream/<token>/seek")
def stream_seek(token: str):
    """Restart FFmpeg at a new position so the user can jump anywhere."""
    d = request.json or {}
    position_s = float(d.get("position_s", 0))
    stream_url = web_player.seek_session(token, position_s)
    if stream_url is None:
        abort(404)
    return jsonify(stream_url=stream_url)


@bp.post("/stream/<token>/position")
def stream_save_position(token: str):
    d = request.json or {}
    user_id = flask_session.get("user_id")
    if not user_id:
        abort(401)
    db.save_playback_position(
        user_id    = user_id,
        token      = token,
        position_s = float(d.get("position_s", 0)),
        duration_s = d.get("duration_s"),
    )
    return jsonify(ok=True)


_INSTALLER_SCRIPT = r"""#!/usr/bin/env bash
# Mycelium native player installer for macOS
# Registers the mycelium:// URL scheme  -  opens streams in IINA, mpv, or VLC.
set -euo pipefail

APP_NAME="MyceliumPlayer"
APP_DIR="/Applications/${APP_NAME}.app"
CONTENTS="${APP_DIR}/Contents"
MACOS="${CONTENTS}/MacOS"
RES="${CONTENTS}/Resources"

echo "Installing ${APP_NAME}.app …"

# ── App bundle skeleton ─────────────────────────────────────────────────────
mkdir -p "${MACOS}" "${RES}"

# ── Handler script ──────────────────────────────────────────────────────────
cat > "${MACOS}/${APP_NAME}" << 'HANDLER'
#!/usr/bin/env bash
# $1 = mycelium://play?url=<encoded-url>
RAW_SCHEME="${1:-}"
if [ -z "${RAW_SCHEME}" ]; then exit 1; fi

# Extract the video URL from the mycelium://play?url=... argument
VIDEO_URL=$(python3 -c "
import sys, urllib.parse
raw = sys.argv[1]
qs  = urllib.parse.urlparse(raw).query
print(urllib.parse.parse_qs(qs).get('url', [''])[0])
" "${RAW_SCHEME}")

if [ -z "${VIDEO_URL}" ]; then exit 1; fi

# Player preference: IINA > mpv > VLC
if [ -d "/Applications/IINA.app" ]; then
    ENCODED=$(python3 -c "import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1],safe=''))" "${VIDEO_URL}")
    open "iina://open?url=${ENCODED}"
elif command -v mpv &>/dev/null; then
    exec mpv --no-terminal "${VIDEO_URL}"
elif [ -d "/Applications/VLC.app" ]; then
    open -a VLC "${VIDEO_URL}"
else
    osascript -e 'display alert "Mycelium Player" message "No supported player found.\nInstall IINA (iina.io) or mpv (mpv.io)."'
fi
HANDLER
chmod +x "${MACOS}/${APP_NAME}"

# ── Info.plist ──────────────────────────────────────────────────────────────
cat > "${CONTENTS}/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>nl.mycelium.player</string>
    <key>CFBundleName</key>
    <string>MyceliumPlayer</string>
    <key>CFBundleDisplayName</key>
    <string>Mycelium Player</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleExecutable</key>
    <string>MyceliumPlayer</string>
    <key>CFBundleURLTypes</key>
    <array>
        <dict>
            <key>CFBundleURLName</key>
            <string>Mycelium stream</string>
            <key>CFBundleURLSchemes</key>
            <array>
                <string>mycelium</string>
            </array>
        </dict>
    </array>
    <key>LSBackgroundOnly</key>
    <true/>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
</dict>
</plist>
PLIST

# ── Register with Launch Services ───────────────────────────────────────────
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -f "${APP_DIR}" 2>/dev/null || true

echo ""
echo "Done! MyceliumPlayer.app installed in /Applications."
echo "The mycelium:// URL scheme is now active."
echo ""
echo "Player priority: IINA > mpv > VLC"
echo "Install IINA from https://iina.io for best results."
"""


@bp.get("/ui/api/web-player/install-macos")
def web_player_install_macos():
    """Download the macOS installer shell script for the mycelium:// URL scheme."""
    return Response(
        _INSTALLER_SCRIPT,
        mimetype="application/x-sh",
        headers={"Content-Disposition": 'attachment; filename="install-mycelium-player.sh"'},
    )
