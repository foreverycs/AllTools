"""Shared small pool for the app's SQLite databases.

Both storage modules (``history``, ``express``) previously re-implemented an
identical connection cache: a per-path persistent connection (WAL,
``synchronous=NORMAL``) so repeated calls skip connect/PRAGMA/DDL setup. This
module is the single home for that logic so the two layers cannot drift.

Connections use ``isolation_level=None`` (autocommit): a statement that fails
midway never leaves an implicit transaction open on a connection that will be
reused, which prevents stale commits and WAL write-lock retention on a pooled
connection.

Callers must serialize access to a cache instance — each storage module already
holds its own module ``_lock`` around every ``get``/``close_all`` call.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict


class ConnCache:
    """Small per-path pool of persistent SQLite connections."""

    _MAX = 4

    def __init__(self) -> None:
        self._cache: Dict[str, sqlite3.Connection] = {}

    def get(self, db_path: Path, schema: str) -> sqlite3.Connection:
        """Return a reusable connection for ``db_path``.

        When the cache is full (e.g. tests rotating tmp dirs) the oldest
        connection is closed to bound open file handles.
        """
        key = str(db_path)
        conn = self._cache.get(key)
        if conn is None:
            conn = sqlite3.connect(key, timeout=10, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.row_factory = sqlite3.Row
            conn.executescript(schema)
            if len(self._cache) >= self._MAX:
                old_path, old_conn = next(iter(self._cache.items()))
                try:
                    old_conn.close()
                except sqlite3.Error:
                    pass
                del self._cache[old_path]
            self._cache[key] = conn
        return conn

    def close_all(self) -> None:
        """Close cached connections (app shutdown / tests). Safe when idle."""
        for conn in self._cache.values():
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._cache.clear()
