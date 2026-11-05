import { requireNonEmptyString, requireString, isoNow } from './validation.js';

export class AppSecretsRepository {
  constructor(db, { now = () => new Date() } = {}) {
    this.db = db;
    this.now = now;
  }

  get(name) {
    requireNonEmptyString(name, 'name');
    const row = this.db.prepare('SELECT value FROM app_secrets WHERE name = ?').get(name);
    return row ? row.value : null;
  }

  set(name, value) {
    requireNonEmptyString(name, 'name');
    requireString(value, 'value');
    const at = isoNow(this.now);
    this.db
      .prepare(`
        INSERT INTO app_secrets (name, value, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
      `)
      .run(name, value, at, at);
    return value;
  }

  /**
   * Atomically return an existing secret or store the value produced by
   * `generate`. Concurrent callers in the same process get the stored value.
   */
  getOrCreate(name, generate) {
    requireNonEmptyString(name, 'name');
    if (typeof generate !== 'function') {
      throw new TypeError('generate must be a function.');
    }
    const existing = this.get(name);
    if (existing !== null && existing !== '') {
      return existing;
    }
    const at = isoNow(this.now);
    this.db
      .prepare(`
        INSERT INTO app_secrets (name, value, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (name) DO NOTHING
      `)
      .run(name, String(generate()), at, at);
    const value = this.get(name);
    if (value === null || value === '') {
      throw new Error(`Secret ${name} could not be created.`);
    }
    return value;
  }

  delete(name) {
    requireNonEmptyString(name, 'name');
    return this.db.prepare('DELETE FROM app_secrets WHERE name = ?').run(name).changes > 0;
  }
}
