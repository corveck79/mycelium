# Parked — no migrations needed for the web player.
#
# The existing virtual_items table has everything required:
#   source    = 'web_player'  →  distinguishes browser tokens
#   strm_path = NULL          →  browser tokens don't write .strm files
#   imdb_id / season / episode / quality  →  already present
#
# Optional later addition (resume position):
#
# MIGRATION_PLAYBACK_SESSIONS = """
# CREATE TABLE IF NOT EXISTS playback_sessions (
#     id         INTEGER PRIMARY KEY AUTOINCREMENT,
#     user_id    INTEGER NOT NULL,
#     token      TEXT    NOT NULL,
#     position_s REAL    NOT NULL DEFAULT 0,
#     duration_s REAL,
#     updated_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now')),
#     UNIQUE(user_id, token)
# )
# """
#
# def save_playback_position(user_id, token, position_s, duration_s=None):
#     with _conn() as c:
#         c.execute("""
#             INSERT INTO playback_sessions (user_id, token, position_s, duration_s)
#             VALUES (?, ?, ?, ?)
#             ON CONFLICT(user_id, token) DO UPDATE
#               SET position_s=excluded.position_s,
#                   duration_s=COALESCE(excluded.duration_s, duration_s),
#                   updated_at=strftime('%Y-%m-%d %H:%M:%S','now')
#         """, (user_id, token, position_s, duration_s))
#
# def get_playback_position(user_id, token):
#     with _conn() as c:
#         row = c.execute(
#             "SELECT position_s FROM playback_sessions WHERE user_id=? AND token=?",
#             (user_id, token),
#         ).fetchone()
#     return row["position_s"] if row else 0.0
