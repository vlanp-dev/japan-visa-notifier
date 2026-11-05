import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { migrate, withBusyRetry } from './migrations.js';

/**
 * Open the SQLite database, apply PRAGMAs and run idempotent migrations.
 * `:memory:` is supported for tests. Lock contention during startup (WAL switch
 * or a concurrent migration) is retried so two replicas can start together.
 */
export function openDatabase(databasePath, { migrations } = {}) {
  if (typeof databasePath !== 'string' || databasePath.trim() === '') {
    throw new TypeError('databasePath must be a non-empty string.');
  }

  if (databasePath !== ':memory:') {
    mkdirSync(path.dirname(databasePath), { recursive: true });
  }

  const db = new DatabaseSync(databasePath);
  try {
    db.exec('PRAGMA foreign_keys = ON');
    db.exec('PRAGMA busy_timeout = 5000');
    if (databasePath !== ':memory:') {
      withBusyRetry(() => db.exec('PRAGMA journal_mode = WAL'));
      db.exec('PRAGMA synchronous = NORMAL');
    }
    migrate(db, migrations);
  } catch (error) {
    try {
      db.close();
    } catch {
      // Keep the original failure.
    }
    throw error;
  }
  return db;
}
