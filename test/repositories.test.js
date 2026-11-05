import assert from 'node:assert/strict';
import test from 'node:test';
import { openTestDatabase } from './helpers.js';

const NOW = new Date('2026-11-04T10:00:00.000Z');
const LATER = new Date('2026-11-04T11:00:00.000Z');

function clock(...dates) {
  let index = 0;
  return () => dates[Math.min(index++, dates.length - 1)];
}

test('settings repository upserts, lists and deletes', (t) => {
  const { repositories } = openTestDatabase(t, { now: () => NOW });
  const { settings } = repositories;

  assert.equal(settings.get('sender'), null);
  settings.set('sender', 'alertes@vlanp.com');
  settings.set('recipient', 'operateur@example.test');
  assert.equal(settings.get('sender'), 'alertes@vlanp.com');
  assert.deepEqual(settings.getAll(), {
    recipient: 'operateur@example.test',
    sender: 'alertes@vlanp.com',
  });

  settings.set('sender', 'noreply@vlanp.com');
  assert.equal(settings.get('sender'), 'noreply@vlanp.com');

  assert.deepEqual(settings.setMany({ recipient: 'ops@example.test', subdomain: 'alertes' }), {
    recipient: 'ops@example.test',
    sender: 'noreply@vlanp.com',
    subdomain: 'alertes',
  });

  assert.equal(settings.delete('subdomain'), true);
  assert.equal(settings.delete('subdomain'), false);
  assert.equal(settings.get('subdomain'), null);
});

test('settings repository validates input and leaves data untouched on error', (t) => {
  const { repositories } = openTestDatabase(t);
  const { settings } = repositories;
  settings.set('sender', 'keep-me');
  assert.throws(() => settings.set('', 'x'), TypeError);
  assert.throws(() => settings.set('key', 42), TypeError);
  assert.throws(() => settings.setMany({ ok: 'yes', bad: 42 }), TypeError);
  assert.deepEqual(settings.getAll(), { sender: 'keep-me' });
  assert.equal(settings.set('blank', ''), '');
  assert.equal(settings.get('blank'), '');
});

test('app secrets are created once and can be read and replaced', (t) => {
  const { repositories } = openTestDatabase(t);
  const { appSecrets } = repositories;
  let generated = 0;
  const generate = () => {
    generated += 1;
    return `secret-${generated}`;
  };

  assert.equal(appSecrets.getOrCreate('ack', generate), 'secret-1');
  assert.equal(appSecrets.getOrCreate('ack', generate), 'secret-1');
  assert.equal(generated, 1);
  assert.equal(appSecrets.get('ack'), 'secret-1');

  appSecrets.set('ack', 'secret-replaced');
  assert.equal(appSecrets.get('ack'), 'secret-replaced');
  assert.equal(appSecrets.delete('ack'), true);
  assert.equal(appSecrets.get('ack'), null);
});

test('episodes are stored with all their rules in one call', (t) => {
  const { repositories } = openTestDatabase(t, { now: () => NOW });
  const { episodes } = repositories;

  const episode = episodes.createWithRules({
    embassyName: 'Ambassade de France',
    embassyPageUrl: 'https://example.test/ambassade',
    excerpt: 'Le contenu a changé.',
    contentHash: 'abc123',
    rules: [
      { ruleKey: 'visa', label: 'Section visa modifiée', detail: 'texte ajouté' },
      { ruleKey: 'contact', label: 'Contact modifié' },
    ],
  });

  assert.equal(episode.id, 1);
  assert.equal(episode.status, 'open');
  assert.equal(episode.openedAt, NOW.toISOString());
  assert.equal(episode.acknowledgedAt, null);
  assert.equal(episode.embassyName, 'Ambassade de France');
  assert.equal(episode.embassyPageUrl, 'https://example.test/ambassade');
  assert.equal(episode.excerpt, 'Le contenu a changé.');
  assert.equal(episode.contentHash, 'abc123');

  const withRules = episodes.getWithRules(episode.id);
  assert.equal(withRules.rules.length, 2);
  assert.equal(withRules.rules[0].ruleKey, 'visa');
  assert.equal(withRules.rules[0].detail, 'texte ajouté');
  assert.equal(withRules.rules[1].ruleKey, 'contact');
  assert.equal(withRules.rules[1].detail, null);

  assert.deepEqual(episodes.list().map((row) => row.id), [1]);
  assert.equal(episodes.getById(999), null);
  assert.equal(episodes.getWithRules(999), null);
});

test('invalid or duplicate episode rules are rejected atomically', (t) => {
  const { repositories, db } = openTestDatabase(t);
  const { episodes } = repositories;

  assert.throws(
    () => episodes.createWithRules({ rules: [{ ruleKey: 'a', label: 'A' }, { ruleKey: 'a', label: 'B' }] }),
    /Duplicate rule key/,
  );
  assert.throws(() => episodes.createWithRules({ rules: [] }), TypeError);
  assert.throws(() => episodes.createWithRules({ rules: [{ ruleKey: '', label: 'A' }] }), TypeError);
  assert.throws(() => episodes.createWithRules({ rules: [{ ruleKey: 'a', label: '' }] }), TypeError);
  assert.equal(db.prepare('SELECT COUNT(*) AS count FROM episodes').get().count, 0);
  assert.equal(db.prepare('SELECT COUNT(*) AS count FROM episode_rules').get().count, 0);

  episodes.createWithRules({ rules: [{ ruleKey: 'a', label: 'A' }] });
  assert.throws(
    () => episodes.createWithRules({ rules: [{ ruleKey: 'b', label: 'B' }, { ruleKey: 'b', label: 'C' }] }),
    /Duplicate rule key/,
  );
  assert.equal(db.prepare('SELECT COUNT(*) AS count FROM episodes').get().count, 1);
});

test('episode rules repository lists and inserts rules independently', (t) => {
  const { repositories } = openTestDatabase(t);
  const { episodes, episodeRules } = repositories;
  const episode = episodes.createWithRules({ rules: [{ ruleKey: 'a', label: 'A' }] });

  const created = episodeRules.create(episode.id, {
    ruleKey: 'b',
    label: 'B',
    detail: 'détail',
    createdAt: NOW.toISOString(),
  });
  assert.equal(created.ruleKey, 'b');
  assert.deepEqual(episodeRules.listByEpisode(episode.id).map((rule) => rule.ruleKey), ['a', 'b']);
  assert.throws(() => episodeRules.create(episode.id, { ruleKey: 'a', label: 'A again' }));
});

test('acknowledgement transitions an episode exactly once', (t) => {
  const { repositories } = openTestDatabase(t, { now: clock(NOW, LATER) });
  const { episodes } = repositories;
  const episode = episodes.createWithRules({ rules: [{ ruleKey: 'a', label: 'A' }] });

  assert.equal(episodes.acknowledge(episode.id), true);
  const acknowledged = episodes.getById(episode.id);
  assert.equal(acknowledged.status, 'acknowledged');
  assert.equal(acknowledged.acknowledgedAt, LATER.toISOString());
  assert.equal(episodes.acknowledge(episode.id), false);
  assert.equal(episodes.acknowledge(999), false);
});

test('email attempts persist accepted, refused and not_attempted states', (t) => {
  const { repositories } = openTestDatabase(t, { now: () => NOW });
  const { episodes, emailAttempts } = repositories;

  const acceptedEpisode = episodes.createWithRules({ rules: [{ ruleKey: 'a', label: 'A' }] });
  const refusedEpisode = episodes.createWithRules({ rules: [{ ruleKey: 'b', label: 'B' }] });
  const skippedEpisode = episodes.createWithRules({ rules: [{ ruleKey: 'c', label: 'C' }] });

  const accepted = emailAttempts.create({
    episodeId: acceptedEpisode.id,
    status: 'accepted',
    providerMessageId: 'resend-id-1',
  });
  assert.equal(accepted.status, 'accepted');
  assert.equal(accepted.providerMessageId, 'resend-id-1');
  assert.equal(accepted.reason, null);
  assert.equal(accepted.attemptedAt, NOW.toISOString());

  const refused = emailAttempts.create({
    episodeId: refusedEpisode.id,
    status: 'refused',
    reason: 'HTTP 401 : clé API invalide',
  });
  assert.equal(refused.status, 'refused');
  assert.equal(refused.reason, 'HTTP 401 : clé API invalide');

  const skipped = emailAttempts.create({
    episodeId: skippedEpisode.id,
    status: 'not_attempted',
    reason: 'Configuration email incomplète : clé absente',
  });
  assert.equal(skipped.status, 'not_attempted');

  assert.equal(emailAttempts.findByEpisodeId(acceptedEpisode.id).status, 'accepted');
  assert.equal(emailAttempts.findByEpisodeId(skippedEpisode.id).status, 'not_attempted');
  assert.deepEqual(
    emailAttempts.list().map((attempt) => attempt.status),
    ['not_attempted', 'refused', 'accepted'],
  );
});

test('email attempts validate states, reasons and one attempt per episode', (t) => {
  const { repositories } = openTestDatabase(t);
  const { episodes, emailAttempts } = repositories;
  const episode = episodes.createWithRules({ rules: [{ ruleKey: 'a', label: 'A' }] });

  assert.throws(() => emailAttempts.create({ episodeId: episode.id, status: 'sent' }), TypeError);
  assert.throws(() => emailAttempts.create({ episodeId: episode.id, status: 'refused' }), TypeError);
  assert.throws(() => emailAttempts.create({ episodeId: episode.id, status: 'not_attempted' }), TypeError);
  assert.throws(
    () => emailAttempts.create({ episodeId: episode.id, status: 'accepted', reason: 'odd' }),
    TypeError,
  );
  assert.throws(() => emailAttempts.create({ status: 'accepted' }), TypeError);

  emailAttempts.create({ episodeId: episode.id, status: 'not_attempted', reason: 'clé absente' });
  assert.throws(
    () => emailAttempts.create({ episodeId: episode.id, status: 'accepted' }),
    (error) => /UNIQUE|constraint/i.test(error.message),
  );

  emailAttempts.create({ kind: 'test', status: 'refused', reason: 'clé invalide' });
  emailAttempts.create({ kind: 'test', status: 'refused', reason: 'clé invalide encore' });
  emailAttempts.create({ kind: 'test', status: 'accepted' });
  assert.throws(
    () => emailAttempts.create({ kind: 'test', episodeId: episode.id, status: 'accepted' }),
    TypeError,
  );
});

test('delivery claims reserve one episode and complete with the persisted attempt', (t) => {
  const { repositories } = openTestDatabase(t, { now: () => NOW });
  const { episodes, emailAttempts, emailDeliveryClaims } = repositories;

  const firstEpisode = episodes.createWithRules({ rules: [{ ruleKey: 'a', label: 'A' }] });
  const firstClaim = emailDeliveryClaims.claim(firstEpisode.id, { claimToken: 'claim-a' });
  assert.equal(firstClaim.claimed, true);
  assert.equal(firstClaim.claim.pending, true);
  assert.equal(firstClaim.claim.claimToken, 'claim-a');

  const duplicateClaim = emailDeliveryClaims.claim(firstEpisode.id, { claimToken: 'claim-b' });
  assert.equal(duplicateClaim.claimed, false);
  assert.equal(duplicateClaim.claim.claimToken, 'claim-a');

  const completed = emailDeliveryClaims.complete({
    episodeId: firstEpisode.id,
    claimToken: 'claim-a',
    status: 'accepted',
    providerMessageId: 'resend-claim-a',
  });
  assert.equal(completed.created, true);
  assert.equal(completed.completed, true);
  assert.equal(completed.attempt.status, 'accepted');
  assert.equal(completed.attempt.providerMessageId, 'resend-claim-a');
  assert.equal(emailDeliveryClaims.findByEpisodeId(firstEpisode.id).outcome, 'accepted');

  const racedEpisode = episodes.createWithRules({ rules: [{ ruleKey: 'b', label: 'B' }] });
  emailDeliveryClaims.claim(racedEpisode.id, { claimToken: 'claim-c' });
  emailAttempts.create({
    episodeId: racedEpisode.id,
    status: 'refused',
    reason: 'HTTP 401 : clé invalide',
  });
  const raced = emailDeliveryClaims.complete({
    episodeId: racedEpisode.id,
    claimToken: 'claim-c',
    status: 'accepted',
    providerMessageId: 'resend-too-late',
  });
  assert.equal(raced.created, false);
  assert.equal(raced.attempt.status, 'refused');
  assert.equal(raced.attempt.reason, 'HTTP 401 : clé invalide');
  assert.equal(emailDeliveryClaims.findByEpisodeId(racedEpisode.id).outcome, 'refused');
});

test('refused count follows the last accepted send and ignores not_attempted', (t) => {
  const { repositories } = openTestDatabase(t);
  const { episodes, emailAttempts } = repositories;

  const first = episodes.createWithRules({ rules: [{ ruleKey: 'a', label: 'A' }] });
  const second = episodes.createWithRules({ rules: [{ ruleKey: 'b', label: 'B' }] });
  const third = episodes.createWithRules({ rules: [{ ruleKey: 'c', label: 'C' }] });
  const fourth = episodes.createWithRules({ rules: [{ ruleKey: 'd', label: 'D' }] });
  const fifth = episodes.createWithRules({ rules: [{ ruleKey: 'e', label: 'E' }] });

  assert.equal(emailAttempts.countRefusedSinceLastAccepted(), 0);
  emailAttempts.create({ episodeId: first.id, status: 'refused', reason: 'refus 1' });
  emailAttempts.create({ episodeId: second.id, status: 'not_attempted', reason: 'clé absente' });
  emailAttempts.create({ episodeId: third.id, status: 'refused', reason: 'refus 2' });
  assert.equal(emailAttempts.countRefusedSinceLastAccepted(), 2);
  assert.equal(emailAttempts.countRefusedSinceLastAccepted({ kind: 'episode' }), 2);

  emailAttempts.create({ kind: 'test', status: 'accepted' });
  assert.equal(emailAttempts.countRefusedSinceLastAccepted(), 0);
  assert.equal(emailAttempts.countRefusedSinceLastAccepted({ kind: 'episode' }), 2);

  emailAttempts.create({ episodeId: fourth.id, status: 'accepted' });
  assert.equal(emailAttempts.countRefusedSinceLastAccepted(), 0);
  assert.equal(emailAttempts.countRefusedSinceLastAccepted({ kind: 'episode' }), 0);

  emailAttempts.create({ episodeId: fifth.id, status: 'refused', reason: 'refus 3' });
  emailAttempts.create({ kind: 'test', status: 'refused', reason: 'test refusé' });
  assert.equal(emailAttempts.countRefusedSinceLastAccepted(), 2);
  assert.equal(emailAttempts.countRefusedSinceLastAccepted({ kind: 'episode' }), 1);
  assert.equal(emailAttempts.countRefusedSinceLastAccepted({ kind: 'test' }), 1);
});
