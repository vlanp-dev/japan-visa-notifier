import assert from 'node:assert/strict';
import test from 'node:test';
import { AckTokenSigner } from '../src/ack/ack-token-signer.js';
import { EpisodeService } from '../src/services/episode-service.js';
import { openTestDatabase } from './helpers.js';

const NOW = new Date('2026-11-04T10:00:00.000Z');
const SECRET = 'episode-service-test-secret-long-enough';
const BASE_URL = 'https://alertes.example.test';

function setup(t, { ttlMs = 60_000, generateNonce } = {}) {
  let current = NOW;
  const now = () => current;
  const context = openTestDatabase(t, { now });
  const signer = new AckTokenSigner({
    secret: SECRET,
    publicBaseUrl: BASE_URL,
    ttlMs,
    now,
  });
  const service = new EpisodeService({
    repositories: context.repositories,
    tokenSigner: signer,
    now,
    generateNonce,
  });
  return {
    ...context,
    signer,
    service,
    advanceTo(date) {
      current = date;
    },
  };
}

const validInput = () => ({
  embassyName: 'Ambassade de France',
  embassyPageUrl: 'https://example.test/ambassade',
  excerpt: 'Le contenu de la page a changé.',
  contentHash: 'hash-1',
  rules: [
    { ruleKey: 'visa', label: 'Section visa modifiée', detail: 'texte ajouté' },
    { ruleKey: 'contact', label: 'Contact modifié' },
  ],
});

test('opening an episode persists the episode, every rule and a signed link', (t) => {
  const { repositories, db, service, signer } = setup(t);
  const result = service.openEpisode(validInput());

  assert.equal(result.episode.id, 1);
  assert.equal(result.episode.status, 'open');
  assert.equal(result.episode.acknowledgedAt, null);
  assert.equal(result.episode.openedAt, NOW.toISOString());
  assert.equal(result.episode.embassyName, 'Ambassade de France');
  assert.equal(result.episode.excerpt, 'Le contenu de la page a changé.');
  assert.equal(result.episode.rules.length, 2);
  assert.equal(result.episode.rules[0].ruleKey, 'visa');
  assert.equal(result.episode.rules[1].label, 'Contact modifié');
  assert.equal(result.expiresAt, new Date(NOW.getTime() + 60_000).toISOString());
  assert.equal(result.episode.ackTokenExpiresAt, result.expiresAt);
  assert.equal(typeof result.episode.ackNonce, 'string');

  const stored = repositories.episodes.getWithRules(result.episode.id);
  assert.equal(stored.ackNonce, result.episode.ackNonce);
  assert.equal(db.prepare('SELECT COUNT(*) AS count FROM episodes').get().count, 1);
  assert.equal(db.prepare('SELECT COUNT(*) AS count FROM episode_rules').get().count, 2);

  const verification = signer.verify(result.token);
  assert.equal(verification.ok, true);
  assert.equal(verification.episodeId, result.episode.id);
  assert.equal(verification.nonce, result.episode.ackNonce);
  assert.equal(verification.expiresAt, result.expiresAt);
  assert.equal(
    result.acknowledgementUrl,
    `${BASE_URL}/ack/${encodeURIComponent(result.token)}`,
  );
});

test('invalid episode input is rejected before anything is written', (t) => {
  const { db, service } = setup(t);
  const cases = [
    {},
    { rules: [] },
    { rules: [{ ruleKey: 'a', label: 'A' }, { ruleKey: 'a', label: 'B' }] },
    { rules: [{ ruleKey: '', label: 'A' }] },
    { rules: [{ ruleKey: 'a', label: '' }] },
    { rules: [{ ruleKey: 'a', label: 'A' }], embassyPageUrl: 'ftp://example.test/page' },
    { rules: [{ ruleKey: 'a', label: 'A' }], embassyPageUrl: 'javascript:alert(1)' },
    { rules: [{ ruleKey: 'a', label: 'A' }], excerpt: 42 },
    { rules: Array.from({ length: 101 }, (_, index) => ({ ruleKey: `r${index}`, label: 'R' })) },
  ];
  for (const input of cases) {
    assert.throws(() => service.openEpisode(input), TypeError);
  }
  assert.equal(db.prepare('SELECT COUNT(*) AS count FROM episodes').get().count, 0);
  assert.equal(db.prepare('SELECT COUNT(*) AS count FROM episode_rules').get().count, 0);
});

test('a failing insert rolls the episode and its rules back together', (t) => {
  const { db, service } = setup(t, { generateNonce: () => 'repeated-nonce' });
  service.openEpisode({ rules: [{ ruleKey: 'a', label: 'A' }] });
  assert.throws(
    () => service.openEpisode({ rules: [{ ruleKey: 'b', label: 'B' }, { ruleKey: 'c', label: 'C' }] }),
    /UNIQUE|constraint/i,
  );
  assert.equal(db.prepare('SELECT COUNT(*) AS count FROM episodes').get().count, 1);
  assert.equal(db.prepare('SELECT COUNT(*) AS count FROM episode_rules').get().count, 1);
});

test('inspection is read-only and acknowledgement transitions open to acknowledged', (t) => {
  const { repositories, service } = setup(t);
  const opened = service.openEpisode(validInput());

  const preview = service.inspectAcknowledgement(opened.token);
  assert.equal(preview.ok, true);
  assert.equal(preview.episode.status, 'open');
  assert.equal(repositories.episodes.getById(opened.episode.id).status, 'open');

  const acknowledged = service.acknowledge(opened.token);
  assert.equal(acknowledged.ok, true);
  assert.equal(acknowledged.episode.status, 'acknowledged');
  assert.equal(acknowledged.episode.acknowledgedAt, NOW.toISOString());
  assert.equal(repositories.episodes.getById(opened.episode.id).status, 'acknowledged');
});

test('replaying an acknowledgement changes nothing and keeps the original timestamp', (t) => {
  const { repositories, service } = setup(t);
  const opened = service.openEpisode(validInput());

  assert.equal(service.acknowledge(opened.token).ok, true);
  const first = repositories.episodes.getById(opened.episode.id);

  const replay = service.acknowledge(opened.token);
  assert.deepEqual(
    { ok: replay.ok, reason: replay.reason },
    { ok: false, reason: 'already_acknowledged' },
  );
  const afterReplay = repositories.episodes.getById(opened.episode.id);
  assert.equal(afterReplay.acknowledgedAt, first.acknowledgedAt);
  assert.equal(service.inspectAcknowledgement(opened.token).reason, 'already_acknowledged');
});

test('a token carrying a different nonce is rejected without changing the episode', (t) => {
  const { repositories, signer, service } = setup(t);
  const opened = service.openEpisode(validInput());
  const forged = signer.issue({
    episodeId: opened.episode.id,
    nonce: 'different-nonce',
    expiresAt: opened.expiresAt,
  });

  assert.deepEqual(service.acknowledge(forged), { ok: false, reason: 'mismatch' });
  assert.equal(repositories.episodes.getById(opened.episode.id).status, 'open');
});

test('a token with a mismatched stored expiry is rejected', (t) => {
  const { repositories, signer, service } = setup(t);
  const opened = service.openEpisode(validInput());
  const forged = signer.issue({
    episodeId: opened.episode.id,
    nonce: opened.episode.ackNonce,
    expiresAt: new Date(Date.parse(opened.expiresAt) + 60_000).toISOString(),
  });

  assert.deepEqual(service.acknowledge(forged), { ok: false, reason: 'mismatch' });
  assert.equal(repositories.episodes.getById(opened.episode.id).status, 'open');
});

test('expired and tampered tokens are rejected without mutating state', (t) => {
  const { repositories, service, advanceTo } = setup(t, { ttlMs: 1000 });
  const opened = service.openEpisode(validInput());

  const tampered = `${opened.token.slice(0, -1)}${opened.token.endsWith('A') ? 'B' : 'A'}`;
  assert.equal(service.acknowledge(tampered).reason, 'invalid_signature');
  assert.equal(service.inspectAcknowledgement(tampered).reason, 'invalid_signature');

  advanceTo(new Date(NOW.getTime() + 1000));
  assert.equal(service.acknowledge(opened.token).reason, 'expired');
  assert.equal(repositories.episodes.getById(opened.episode.id).status, 'open');
  assert.equal(repositories.episodes.getById(opened.episode.id).acknowledgedAt, null);
});

test('a correctly signed token for an unknown episode is rejected', (t) => {
  const { signer, service } = setup(t);
  const token = signer.issue({
    episodeId: 999,
    nonce: 'unknown',
    expiresAt: signer.nextExpiry(),
  });
  assert.deepEqual(service.acknowledge(token), { ok: false, reason: 'not_found' });
});
