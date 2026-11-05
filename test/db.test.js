import assert from 'node:assert/strict';
import test from 'node:test';
import { openDatabase } from '../src/db.js';
import { isBusyError, MIGRATIONS, withBusyRetry } from '../src/migrations.js';
import { makeTempDir } from './helpers.js';
import { join } from 'node:path';

const NOW = '2026-11-04T10:00:00.000Z';

function tempDatabase(t) {
  const dir = makeTempDir(t);
  const databasePath = join(dir, 'alerts.sqlite');
  const db = openDatabase(databasePath);
  t.after(() => {
    try {
      db.close();
    } catch {
      // Already closed by the test.
    }
  });
  return { db, dir, databasePath };
}

function insertEpisode(db, overrides = {}) {
  const result = db
    .prepare(`
      INSERT INTO episodes (opened_at, status, embassy_name, embassy_page_url, excerpt)
      VALUES (?, 'open', 'Ambassade de test', 'https://example.test/ambassade', 'extrait')
    `)
    .run(overrides.openedAt ?? NOW);
  return Number(result.lastInsertRowid);
}

function insertAttempt(db, episodeId, status, reason = null) {
  return db
    .prepare(`
      INSERT INTO email_attempts (kind, episode_id, status, reason, attempted_at)
      VALUES ('episode', ?, ?, ?, ?)
    `)
    .run(episodeId, status, reason, NOW);
}

test('initial migration creates all foundation tables and indexes', (t) => {
  const { db } = tempDatabase(t);
  const tables = db
    .prepare("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
    .all()
    .map((row) => row.name);
  for (const table of [
    'app_secrets',
    'email_attempts',
    'email_delivery_claims',
    'episode_rules',
    'episodes',
    'settings',
  ]) {
    assert.ok(tables.includes(table), `missing table ${table}`);
  }
  assert.ok(tables.includes('schema_migrations'));

  const indexes = db
    .prepare("SELECT name FROM sqlite_master WHERE type = 'index'")
    .all()
    .map((row) => row.name);
  assert.ok(indexes.includes('idx_email_attempts_one_per_episode'));
  assert.ok(indexes.includes('idx_episodes_status'));
  assert.ok(indexes.includes('idx_episodes_ack_nonce'));
  assert.ok(indexes.includes('idx_episode_rules_episode'));

  const episodeColumns = db
    .prepare('PRAGMA table_info(episodes)')
    .all()
    .map((column) => column.name);
  assert.ok(episodeColumns.includes('ack_nonce'));
  assert.ok(episodeColumns.includes('ack_token_expires_at'));

  const partialIndex = db
    .prepare(`
      SELECT sql FROM sqlite_master
      WHERE type = 'index' AND name = 'idx_email_attempts_one_per_episode'
    `)
    .get();
  assert.match(partialIndex.sql, /WHERE kind = 'episode'/i);

  assert.equal(db.prepare('PRAGMA foreign_keys').get().foreign_keys, 1);
  assert.equal(db.prepare('SELECT COUNT(*) AS count FROM schema_migrations').get().count, MIGRATIONS.length);
});

test('startup retries a locked database but rethrows other errors', () => {
  let attempts = 0;
  const value = withBusyRetry(
    () => {
      attempts += 1;
      if (attempts < 3) {
        const error = new Error('database is locked');
        error.code = 'SQLITE_BUSY';
        throw error;
      }
      return 'opened';
    },
    { attempts: 5, delayMs: 1 },
  );
  assert.equal(value, 'opened');
  assert.equal(attempts, 3);

  let failures = 0;
  assert.throws(
    () =>
      withBusyRetry(
        () => {
          failures += 1;
          throw new TypeError('not a lock');
        },
        { attempts: 5, delayMs: 1 },
      ),
    TypeError,
  );
  assert.equal(failures, 1);
  assert.equal(isBusyError(new Error('database table is locked')), true);
  assert.equal(isBusyError(new TypeError('not a lock')), false);
});

test('migrations are idempotent across reopen', (t) => {
  const dir = makeTempDir(t);
  const databasePath = join(dir, 'alerts.sqlite');
  const first = openDatabase(databasePath);
  first.prepare('INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)').run('sender', 'a', NOW);
  first.close();

  const second = openDatabase(databasePath);
  t.after(() => second.close());
  assert.equal(second.prepare('SELECT COUNT(*) AS count FROM schema_migrations').get().count, MIGRATIONS.length);
  assert.equal(second.prepare('SELECT value FROM settings WHERE key = ?').get('sender').value, 'a');
});

test('email attempt status is constrained to the three persisted states', (t) => {
  const { db } = tempDatabase(t);
  const episodeId = insertEpisode(db);
  assert.throws(() => insertAttempt(db, episodeId, 'delivered'));
  assert.throws(() => insertAttempt(db, episodeId, 'sent'));
});

test('only one episode email attempt is allowed, while test attempts are unlimited', (t) => {
  const { db } = tempDatabase(t);
  const episodeId = insertEpisode(db);
  insertAttempt(db, episodeId, 'accepted');
  assert.throws(
    () => insertAttempt(db, episodeId, 'accepted'),
    (error) => /UNIQUE|constraint/i.test(error.message),
  );

  const insertTest = db.prepare(`
    INSERT INTO email_attempts (kind, episode_id, status, reason, attempted_at)
    VALUES ('test', NULL, ?, ?, ?)
  `);
  insertTest.run('refused', 'invalid key', NOW);
  insertTest.run('refused', 'invalid key again', NOW);
  insertTest.run('accepted', null, NOW);
  assert.equal(db.prepare("SELECT COUNT(*) AS count FROM email_attempts WHERE kind = 'test'").get().count, 3);
});

test('the partial unique index also blocks a second attempt after not_attempted', (t) => {
  const { db } = tempDatabase(t);
  const episodeId = insertEpisode(db);
  insertAttempt(db, episodeId, 'not_attempted', 'configuration incomplète');
  assert.throws(() => insertAttempt(db, episodeId, 'accepted'));
});

test('refused and not_attempted attempts require a reason', (t) => {
  const { db } = tempDatabase(t);
  assert.throws(() => insertAttempt(db, insertEpisode(db), 'refused'));
  assert.throws(() => insertAttempt(db, insertEpisode(db), 'not_attempted'));
  insertAttempt(db, insertEpisode(db), 'refused', 'HTTP 401');
  insertAttempt(db, insertEpisode(db), 'not_attempted', 'clé absente');
});

test('delivery claims are unique per episode, consistent and cascade with the episode', (t) => {
  const { db } = tempDatabase(t);
  const episodeId = insertEpisode(db);
  const insertClaim = db.prepare(`
    INSERT INTO email_delivery_claims (episode_id, claim_token, claimed_at)
    VALUES (?, ?, ?)
  `);
  insertClaim.run(episodeId, 'claim-1', NOW);
  assert.throws(() => insertClaim.run(episodeId, 'claim-2', NOW), /UNIQUE|constraint/i);
  assert.throws(
    () =>
      db
        .prepare('UPDATE email_delivery_claims SET outcome = ? WHERE episode_id = ?')
        .run('accepted', episodeId),
    /constraint/i,
  );

  db.prepare('DELETE FROM episodes WHERE id = ?').run(episodeId);
  assert.equal(db.prepare('SELECT COUNT(*) AS count FROM email_delivery_claims').get().count, 0);
});

test('foreign keys protect episodes and cascade to rules and attempts', (t) => {
  const { db } = tempDatabase(t);
  assert.throws(() => insertAttempt(db, 9999, 'accepted'));

  const episodeId = insertEpisode(db);
  db.prepare(`
    INSERT INTO episode_rules (episode_id, rule_key, label, created_at)
    VALUES (?, ?, ?, ?)
  `).run(episodeId, 'rule-a', 'Règle A', NOW);
  insertAttempt(db, episodeId, 'accepted');

  db.prepare('DELETE FROM episodes WHERE id = ?').run(episodeId);
  assert.equal(db.prepare('SELECT COUNT(*) AS count FROM episode_rules').get().count, 0);
  assert.equal(db.prepare('SELECT COUNT(*) AS count FROM email_attempts').get().count, 0);
});
