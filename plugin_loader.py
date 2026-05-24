"""
Plugin loader — scans plugins/ at startup and registers Flask blueprints.

Plugin structure:
    plugins/
    └── myplugin/
        ├── __init__.py   required: exposes `blueprint` and `PLUGIN_META`
        └── ...

PLUGIN_META dict:
    name         str   machine name (auto-set from folder name)
    label        str   human label shown in UI
    version      str
    description  str
    user_fields  list[str]   columns this plugin adds to the users table

Activating a plugin: drop its folder into plugins/ and restart.
Deactivating:        remove the folder and restart.
"""
import importlib
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_loaded: dict[str, dict] = {}   # name → {"meta": dict, "module": module}


def load_all(app) -> None:
    plugins_dir = Path(__file__).parent / "plugins"
    if not plugins_dir.exists():
        return
    for entry in sorted(plugins_dir.iterdir()):
        if entry.is_dir() and not entry.name.startswith("_") and (entry / "__init__.py").exists():
            _load_one(app, entry.name)


def _load_one(app, name: str) -> None:
    try:
        mod = importlib.import_module(f"plugins.{name}")

        meta = dict(getattr(mod, "PLUGIN_META", {}))
        meta["name"] = name

        if hasattr(mod, "run_migrations"):
            mod.run_migrations()

        bp = getattr(mod, "blueprint", None)
        if bp is not None:
            app.register_blueprint(bp)

        _loaded[name] = {"meta": meta, "module": mod}
        log.info("Plugin loaded: %s v%s", name, meta.get("version", "?"))

    except Exception:
        log.exception("Plugin failed to load: %s", name)


def loaded_plugins() -> list[dict]:
    return [p["meta"] for p in _loaded.values()]


def is_loaded(name: str) -> bool:
    return name in _loaded


def session_fields(user_record: dict) -> dict:
    """Extra fields each plugin contributes to the session response."""
    extra: dict = {}
    for p in _loaded.values():
        if hasattr(p["module"], "session_data"):
            extra.update(p["module"].session_data(user_record))
    return extra


def user_fields() -> list[str]:
    """All user-table columns added by loaded plugins (user_fields + admin_fields)."""
    fields: list[str] = []
    for p in _loaded.values():
        fields.extend(p["meta"].get("user_fields", []))
        fields.extend(p["meta"].get("admin_fields", []))
    return fields


def register_jobs(scheduler) -> None:
    """Call after the APScheduler is started."""
    for name, p in _loaded.items():
        if hasattr(p["module"], "register_jobs"):
            try:
                p["module"].register_jobs(scheduler)
                log.info("Plugin jobs registered: %s", name)
            except Exception:
                log.exception("Plugin job registration failed: %s", name)
