"""Process-wide serialization of reference reads and writes.

A check and a refreeze must never overlap: otherwise a check that started
before a refreeze could persist a stale change, or an initial capture could
silently supersede the reference the operator just confirmed.  The scheduler
thread and the HTTP handlers live in the same process, so one re-entrant lock
per database path is enough.  Cross-process writes still rely on SQLite
transactions and the compare-and-swap in :meth:`Store.record_check`.
"""

from __future__ import annotations

import os
import threading

_LOCKS: dict[str, threading.RLock] = {}
_GUARD = threading.Lock()


def reference_lock(database_path: str) -> threading.RLock:
    """Return the shared lock for ``database_path``."""

    key = _lock_key(database_path)
    with _GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def _lock_key(database_path: str) -> str:
    if not isinstance(database_path, str) or not database_path:
        raise ValueError("database_path must be a non-empty string")
    if database_path == ":memory:":
        return database_path
    return os.path.abspath(os.path.expanduser(database_path))


__all__ = ["reference_lock"]
