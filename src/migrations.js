/**
 * Ordered, append-only schema migrations. Each migration runs once inside a
 * transaction and is recorded in schema_migrations. Never edit an applied
 * migration; add a new one instead.
 */
export const MIGRATIONS = Object.freeze([
  {
    version: 1,
    name: 'initial schema',
    up: `
      CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );

      CREATE TABLE app_secrets (
        name TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );

      CREATE TABLE episodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        opened_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open'
          CHECK (status IN ('open', 'acknowledged')),
        acknowledged_at TEXT,
        embassy_name TEXT,
        embassy_page_url TEXT,
        excerpt TEXT NOT NULL DEFAULT '',
        content_hash TEXT,
        CHECK (
          (status = 'open' AND acknowledged_at IS NULL)
          OR (status = 'acknowledged' AND acknowledged_at IS NOT NULL)
        )
      );

      CREATE INDEX idx_episodes_status ON episodes (status);
      CREATE INDEX idx_episodes_opened_at ON episodes (opened_at);

      CREATE TABLE episode_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        episode_id INTEGER NOT NULL REFERENCES episodes (id) ON DELETE CASCADE,
        rule_key TEXT NOT NULL,
        label TEXT NOT NULL,
        detail TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (episode_id, rule_key)
      );

      CREATE INDEX idx_episode_rules_episode ON episode_rules (episode_id);

      CREATE TABLE email_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL DEFAULT 'episode'
          CHECK (kind IN ('episode', 'test')),
        episode_id INTEGER REFERENCES episodes (id) ON DELETE CASCADE,
        status TEXT NOT NULL
          CHECK (status IN ('accepted', 'refused', 'not_attempted')),
        provider_message_id TEXT,
        reason TEXT,
        attempted_at TEXT NOT NULL,
        CHECK ((kind = 'episode' AND episode_id IS NOT NULL) OR (kind = 'test' AND episode_id IS NULL)),
        CHECK (status <> 'accepted' OR reason IS NULL),
        CHECK (status <> 'refused' OR reason IS NOT NULL),
        CHECK (status <> 'not_attempted' OR reason IS NOT NULL)
      );

      CREATE UNIQUE INDEX idx_email_attempts_one_per_episode
        ON email_attempts (episode_id)
        WHERE kind = 'episode';

      CREATE INDEX idx_email_attempts_episode ON email_attempts (episode_id);
      CREATE INDEX idx_email_attempts_status ON email_attempts (status);
      CREATE INDEX idx_email_attempts_attempted_at ON email_attempts (attempted_at);
      CREATE INDEX idx_email_attempts_kind_status ON email_attempts (kind, status, id);
    `,
  },
]);

export function migrate(db, migrations = MIGRATIONS) {
  db.exec(`
    CREATE TABLE IF NOT EXISTS schema_migrations (
      version INTEGER PRIMARY KEY,
      name TEXT NOT NULL,
      applied_at TEXT NOT NULL
    );
  `);

  const applied = new Set(
    db.prepare('SELECT version FROM schema_migrations').all().map((row) => row.version),
  );

  for (const migration of migrations) {
    if (applied.has(migration.version)) {
      continue;
    }
    if (!Number.isInteger(migration.version) || migration.version < 1) {
      throw new Error('Migration versions must be positive integers.');
    }
    db.exec('BEGIN IMMEDIATE');
    try {
      db.exec(migration.up);
      db.prepare(
        'INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)',
      ).run(migration.version, migration.name, new Date().toISOString());
      db.exec('COMMIT');
    } catch (error) {
      try {
        db.exec('ROLLBACK');
      } catch {
        // Preserve the original migration error.
      }
      throw error;
    }
  }
  return applied.size;
}
