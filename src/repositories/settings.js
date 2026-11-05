import { requireNonEmptyString, requireString, isoNow } from './validation.js';

export class SettingsRepository {
  constructor(db, { now = () => new Date() } = {}) {
    this.db = db;
    this.now = now;
  }

  get(key) {
    requireNonEmptyString(key, 'key');
    const row = this.db.prepare('SELECT value FROM settings WHERE key = ?').get(key);
    return row ? row.value : null;
  }

  getAll() {
    const rows = this.db.prepare('SELECT key, value FROM settings ORDER BY key').all();
    return Object.fromEntries(rows.map((row) => [row.key, row.value]));
  }

  set(key, value) {
    requireNonEmptyString(key, 'key');
    requireString(value, 'value');
    this.db
      .prepare(`
        INSERT INTO settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
      `)
      .run(key, value, isoNow(this.now));
    return value;
  }

  setMany(entries) {
    if (entries === null || typeof entries !== 'object' || Array.isArray(entries)) {
      throw new TypeError('entries must be an object mapping keys to values.');
    }
    const normalized = Object.entries(entries);
    for (const [key, value] of normalized) {
      requireNonEmptyString(key, 'key');
      requireString(value, 'value');
    }
    if (normalized.length === 0) {
      return this.getAll();
    }
    const at = isoNow(this.now);
    const statement = this.db.prepare(`
      INSERT INTO settings (key, value, updated_at)
      VALUES (?, ?, ?)
      ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
    `);
    this.db.exec('BEGIN IMMEDIATE');
    try {
      for (const [key, value] of normalized) {
        statement.run(key, value, at);
      }
      this.db.exec('COMMIT');
    } catch (error) {
      try {
        this.db.exec('ROLLBACK');
      } catch {
        // Preserve the original error.
      }
      throw error;
    }
    return this.getAll();
  }

  delete(key) {
    requireNonEmptyString(key, 'key');
    return this.db.prepare('DELETE FROM settings WHERE key = ?').run(key).changes > 0;
  }
}
