"""Dashboard authentication.

Two flavours, both opt-in:

1. Built-in session login. AUTH_USERNAME + a scrypt-hashed AUTH_PASSWORD.
   The wizard collects a plain password and immediately hashes it; the
   plain value is wiped from settings after the hash lands.

2. Reverse-proxy header trust. If you already run Authelia, Authentik,
   Traefik forward-auth or similar in front of Mycelium, set
   TRUSTED_PROXY_AUTH=true and the user from the configured header is
   accepted as authenticated. A network whitelist guards against
   header spoofing from non-proxy clients.

Webhook, /health and /healthz stay unauthenticated so external systems
(Seerr, Synology Container Manager) keep working.
/metrics requires an admin session or a valid METRICS_TOKEN header.
/dav uses HTTP Basic Auth against the Mycelium user database.
/stream/ uses token-based access (token embedded in .strm files).
"""
from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import ipaddress
import logging
import secrets

from flask import jsonify, redirect, request, session, url_for

import settings

log = logging.getLogger(__name__)

_PUBLIC_PATHS = (
    "/webhook",
    "/torbox-webhook",
    "/health",
    "/healthz",
    "/login",
    "/login/oidc",
    "/oidc/callback",
    "/logout",
    "/stream/",
    "/assets",
    "/static",
)


# ─────────────────────────────────────────────────────────────────────────────
# Password hashing
# ─────────────────────────────────────────────────────────────────────────────

def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.scrypt(pw.encode(), salt=salt.encode(), n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt}${h.hex()}"


def _verify_hashed(pw: str, stored: str) -> bool:
    try:
        _, salt, hash_hex = stored.split("$", 2)
        expected = hashlib.scrypt(pw.encode(), salt=salt.encode(),
                                   n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(expected.hex(), hash_hex)
    except Exception:
        return False


def _verify_password(pw: str) -> bool:
    hashed = settings.get("AUTH_PASSWORD_HASH", "")
    if hashed and hashed.startswith("scrypt$"):
        return _verify_hashed(pw, hashed)
    # First-run fallback: AUTH_PASSWORD stored as plain. If it matches, upgrade.
    plain = settings.get("AUTH_PASSWORD", "")
    if plain and hmac.compare_digest(pw, plain):
        settings.set("AUTH_PASSWORD_HASH", hash_password(pw))
        settings.set("AUTH_PASSWORD", None)
        log.info("AUTH_PASSWORD upgraded to scrypt hash")
        return True
    return False


def set_password(pw: str) -> None:
    """Public helper used by the setup wizard / settings UI."""
    settings.set("AUTH_PASSWORD_HASH", hash_password(pw))
    settings.set("AUTH_PASSWORD", None)


# ─────────────────────────────────────────────────────────────────────────────
# Reverse-proxy header trust
# ─────────────────────────────────────────────────────────────────────────────

def _ip_in_trusted(remote: str | None) -> bool:
    if not remote:
        return False
    networks_raw = settings.get("TRUSTED_PROXY_NETWORKS", "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16")
    if isinstance(networks_raw, list):
        nets = networks_raw
    else:
        nets = [n.strip() for n in (networks_raw or "").split(",") if n.strip()]
    try:
        ip = ipaddress.ip_address(remote)
    except ValueError:
        return False
    for n in nets:
        try:
            if ip in ipaddress.ip_network(n, strict=False):
                return True
        except ValueError:
            continue
    return False


def _proxy_user() -> str | None:
    if not settings.get("TRUSTED_PROXY_AUTH", False):
        return None
    if not _ip_in_trusted(request.remote_addr):
        return None
    header = settings.get("TRUSTED_PROXY_USER_HEADER", "X-Forwarded-User")
    return request.headers.get(header) or None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def is_enabled() -> bool:
    if settings.get("AUTH_ENABLED", False):
        return True
    # OIDC implicitly enables auth-gating
    try:
        import oidc
        return oidc.is_enabled()
    except Exception:
        return False


def current_user() -> str | None:
    if not is_enabled():
        return None
    return session.get("user") or _proxy_user()


def current_user_record() -> dict | None:
    """Return the full users-table row for the active session, or None.
    Falls back to a synthetic 'admin' record for the legacy single-user mode."""
    uid = session.get("user_id")
    if uid:
        import db
        u = db.get_user(uid)
        if u:
            return u
    user = current_user()
    if user:
        # Legacy / proxy-auth path: synthesize an admin record
        return {"id": 0, "username": user, "role": "admin", "auto_approve": 1,
                "quota_monthly": 0, "enabled": 1, "webplayer_enabled": 1}
    return None


def is_admin() -> bool:
    # Auth disabled → single-user mode, full admin access.
    if not is_enabled():
        return True
    rec = current_user_record()
    return bool(rec and rec.get("role") == "admin")


def attempt_login(username: str, password: str) -> bool:
    """Authenticate against either the users table (multi-user) or the
    legacy single-user AUTH_USERNAME/AUTH_PASSWORD_HASH settings."""
    # Try DB-backed user first
    try:
        import db
        u = db.get_user_by_username(username)
        if u and u.get("enabled") and u.get("password_hash", "").startswith("scrypt$"):
            if _verify_hashed(password, u["password_hash"]):
                session["user"] = u["username"]
                session["user_id"] = u["id"]
                session["role"] = u["role"]
                db.touch_user_login(u["id"])
                return True
    except Exception as exc:
        log.warning("DB user auth failed: %s", exc)

    # Legacy fallback
    expected_user = settings.get("AUTH_USERNAME", "admin") or "admin"
    if not hmac.compare_digest(username, expected_user):
        return False
    if _verify_password(password):
        session["user"] = expected_user
        session["role"] = "admin"
        return True
    return False


def create_user_account(username: str, password: str, role: str = "user",
                         auto_approve: bool = False) -> int:
    import db
    if db.get_user_by_username(username):
        raise ValueError(f"User '{username}' already exists")
    return db.create_user(username, hash_password(password), role=role,
                          auto_approve=auto_approve)


def change_user_password(user_id: int, new_password: str) -> None:
    import db
    db.update_user(user_id, password_hash=hash_password(new_password))


def require_role(role: str):
    """Decorator: require a specific role (e.g. 'admin')."""
    def deco(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            if not is_enabled():
                return view(*args, **kwargs)
            rec = current_user_record()
            if not rec:
                if request.path.startswith("/ui/api/") or request.headers.get("Accept", "").startswith("application/json"):
                    return jsonify(error="unauthorized"), 401
                return redirect(url_for("login_view", next=request.path))
            if rec.get("role") != role and role != "user":
                return jsonify(error="forbidden"), 403
            return view(*args, **kwargs)
        return wrapped
    return deco


def require_auth(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not is_enabled():
            return view(*args, **kwargs)
        if session.get("user"):
            return view(*args, **kwargs)
        if _proxy_user():
            session["user"] = _proxy_user()
            return view(*args, **kwargs)
        # Not authenticated
        if request.path.startswith("/ui/api/") or request.headers.get("Accept", "").startswith("application/json"):
            return jsonify(error="unauthorized"), 401
        return redirect(url_for("login_view", next=request.path))
    return wrapped


def _enforce_basic_auth():
    """Return a 401 WWW-Authenticate challenge unless valid Basic Auth is provided."""
    header = request.headers.get("Authorization", "")
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            username, password = decoded.split(":", 1)
            import db as _db
            user = _db.get_user_by_username(username)
            if user and user.get("enabled") and _verify_hashed(password, user.get("password_hash", "")):
                return None
        except Exception:
            pass
    from flask import Response
    return Response(
        "Authentication required",
        status=401,
        headers={"WWW-Authenticate": 'Basic realm="Mycelium WebDAV"'},
    )


def install_before_request(app) -> None:
    """Apply auth as a before_request hook so every UI route is covered."""
    @app.before_request
    def _enforce():
        if not is_enabled():
            return None
        path = request.path
        # Public paths are always allowed
        for prefix in _PUBLIC_PATHS:
            if path == prefix or path.startswith(prefix + "/") or path == prefix.rstrip("/"):
                return None
        if path.startswith("/stream/"):
            return None
        if path.startswith("/dav"):
            return _enforce_basic_auth()
        if session.get("user"):
            return None
        proxy_user = _proxy_user()
        if proxy_user:
            session["user"] = proxy_user
            return None
        if path.startswith("/ui/api/") or request.headers.get("Accept", "").startswith("application/json"):
            return jsonify(error="unauthorized"), 401
        if path.startswith("/admin"):
            return redirect(url_for("login_view", next=path))
        return redirect("/login?next=" + path)
