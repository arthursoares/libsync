"""Shared resolution for configured application filesystem paths."""

import os


def resolve_downloads_root(db) -> str:
    """Resolve downloads root: database config, environment, then ``/music``."""
    return (
        db.get_config("downloads_path")
        or os.environ.get("STREAMRIP_DOWNLOADS_PATH")
        or "/music"
    )


def resolve_database_dir(db) -> str:
    """Return the directory holding the app and per-source dedup databases."""
    if db.path != ":memory:":
        return os.path.dirname(db.path) or "."
    configured = os.environ.get("STREAMRIP_DB_PATH", "data/streamrip.db")
    return os.path.dirname(configured) or "data"
