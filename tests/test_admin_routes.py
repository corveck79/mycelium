"""
/ui/logs, /ui/backup-now and /ui/api/backups must be admin-only.

The app starts a scheduler and touches /data on import, so the checks run in a subprocess with a
temporary data dir. That keeps the rest of the suite (which swaps modules in sys.modules) isolated.
"""
import json
import os
import subprocess
import sys

REPO = os.path.join(os.path.dirname(__file__), "..")

_SCRIPT = r"""
import json, os, sys, tempfile
d = tempfile.mkdtemp()
os.environ.update({
    "DB_PATH": d + "/requests.db",
    "MEDIA_PATH": d + "/media",
    "SPORE_MEDIA_PATH": d + "/plex-media",
    "ZILEAN_DB_PATH": d + "/zilean.db",
})
sys.path.insert(0, sys.argv[1])
import app as m
import auth
import backup

m.app.config["WTF_CSRF_ENABLED"] = False
backup.run = lambda *a, **k: None
backup.list_backups = lambda: []
client = m.app.test_client()
out = {}
for admin in (False, True):
    auth.is_admin = lambda a=admin: a
    out[str(admin)] = {
        "logs": client.get("/ui/logs").status_code,
        "backups": client.get("/ui/api/backups").status_code,
        "backup_now": client.post("/ui/backup-now").status_code,
    }
print("RESULT " + json.dumps(out))
"""


def _run():
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT, os.path.abspath(REPO)],
        capture_output=True, text=True, timeout=120,
    )
    lines = [l for l in proc.stdout.splitlines() if l.startswith("RESULT ")]
    assert lines, f"no result from subprocess: {proc.stderr[-800:]}"
    return json.loads(lines[-1][len("RESULT "):])


def test_non_admin_gets_403_on_all_three_routes():
    res = _run()["False"]
    assert res == {"logs": 403, "backups": 403, "backup_now": 403}


def test_admin_can_use_all_three_routes():
    res = _run()["True"]
    assert res["logs"] == 200
    assert res["backups"] == 200
    assert res["backup_now"] in (200, 302)
