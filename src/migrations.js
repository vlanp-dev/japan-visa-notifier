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
  {
    version: 2,
    name: 'acknowledgement token state',
    up: `
      ALTER TABLE episodes ADD COLUMN ack_nonce TEXT;
      ALTER TABLE episodes ADD COLUMN ack_token_expires_at TEXT;

      CREATE UNIQUE INDEX idx_episodes_ack_nonce
        ON episodes (ack_nonce)
        WHERE ack_nonce IS NOT NULL;
    `,
  },
  {
    version: 3,
    name: 'durable email delivery claims',
    up: `
      CREATE TABLE email_delivery_claims (
        episode_id INTEGER PRIMARY KEY REFERENCES episodes (id) ON DELETE CASCADE,
        claim_token TEXT NOT NULL,
        claimed_at TEXT NOT NULL,
        completed_at TEXT,
        outcome TEXT
          CHECK (outcome IS NULL OR outcome IN ('accepted', 'refused', 'not_attempted')),
        CHECK (
          (completed_at IS NULL AND outcome IS NULL)
          OR (completed_at IS NOT NULL AND outcome IS NOT NULL)
        )
      );

      CREATE INDEX idx_email_delivery_claims_completed_at
        ON email_delivery_claims (completed_at);
    `,
  },
  {
    version: 4,
    name: 'email delivery claim start marker',
    up: `
      -- Claims created before this migration were written immediately before
      -- the provider call, so they are treated as already started. Claims
      -- written by the opening transaction keep started_at NULL until a
      -- delivery caller atomically takes them over.
      ALTER TABLE email_delivery_claims ADD COLUMN started_at TEXT;
      UPDATE email_delivery_claims SET started_at = claimed_at WHERE started_at IS NULL;
    `,
  },
]);

const BUSY_RETRY_DELAY_MS = 25;
const BUSY_RETRY_ATTEMPTS = 200;

/** True when SQLite asks the caller to retry because another writer holds a lock. */
export function isBusyError(error) {
  if (error === null || typeof error !== 'object') {
    return false;
  }
  if (error.code === 'SQLITE_BUSY' || error.code === 'SQLITE_LOCKED') {
    return true;
  }
  return typeof error.message === 'string' && /database (?:table )?is locked/i.test(error.message);
}

function sleepSync(ms) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

/** Retry a synchronous SQLite operation while the database is briefly locked. */
export function withBusyRetry(
  operation,
  { attempts = BUSY_RETRY_ATTEMPTS, delayMs = BUSY_RETRY_DELAY_MS } = {},
) {
  let lastError;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      return operation();
    } catch (error) {
      if (!isBusyError(error)) {
        throw error;
      }
      lastError = error;
      sleepSync(delayMs);
    }
  }
  throw lastError;
}

function validateMigrations(migrations) {
  if (!Array.isArray(migrations)) {
    throw new TypeError('migrations must be an array.');
  }
  let previous = 0;
  for (const migration of migrations) {
    if (!Number.isInteger(migration.version) || migration.version < 1) {
      throw new Error('Migration versions must be positive integers.');
    }
    if (migration.version <= previous) {
      throw new Error('Migration versions must be strictly increasing.');
    }
    if (typeof migration.name !== 'string' || migration.name === '') {
      throw new Error('Migration names must be non-empty strings.');
    }
    if (typeof migration.up !== 'string' || migration.up.trim() === '') {
      throw new Error('Migration SQL must be a non-empty string.');
    }
    previous = migration.version;
  }
}

function applyMigration(db, migration) {
  db.exec('BEGIN IMMEDIATE');
  try {
    // Re-check inside the write transaction: another process may have applied
    // this migration between our earlier look and acquiring the write lock.
    const alreadyApplied = db
      .prepare('SELECT 1 AS applied FROM schema_migrations WHERE version = ?')
      .get(migration.version);
    if (!alreadyApplied) {
      db.exec(migration.up);
      db.prepare(
        'INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)',
      ).run(migration.version, migration.name, new Date().toISOString());
    }
    db.exec('COMMIT');
    return !alreadyApplied;
  } catch (error) {
    try {
      db.exec('ROLLBACK');
    } catch {
      // Preserve the original migration error.
    }
    throw error;
  }
}

/**
 * Apply every missing migration. Every version is checked and applied under
 * BEGIN IMMEDIATE, and lock contention is retried, so two processes starting
 * against the same database cannot race the same migration.
 */
export function migrate(db, migrations = MIGRATIONS) {
  validateMigrations(migrations);

  withBusyRetry(() => {
    db.exec(`
      CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        applied_at TEXT NOT NULL
      );
    `);
  });

  let appliedCount = 0;
  for (const migration of migrations) {
    let applied = false;
    // `applyMigration` may fail mid-transaction when another process holds the
    // write lock; retrying the whole transaction also re-checks the version.
    applied = withBusyRetry(() => applyMigration(db, migration));
    if (applied) {
      appliedCount += 1;
    }
  }
  return appliedCount;
}
