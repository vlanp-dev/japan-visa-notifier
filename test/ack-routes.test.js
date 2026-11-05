import assert from 'node:assert/strict';
import test from 'node:test';
import { AckTokenSigner } from '../src/ack/ack-token-signer.js';
import { registerAckRoutes } from '../src/ack/routes.js';
import { EpisodeService } from '../src/services/episode-service.js';
import { createTestHarness } from './helpers.js';

const NOW = new Date('2026-11-04T10:00:00.000Z');
const SECRET = 'ack-routes-test-secret-long-enough';

async function setup(t, { ttlMs = 60_000 } = {}) {
  let current = NOW;
  const now = () => current;
  let service;
  let signer;
  const harness = await createTestHarness(t, {
    registerRoutes: (app, { config, repositories }) => {
      signer = new AckTokenSigner({
        secret: SECRET,
        publicBaseUrl: config.publicBaseUrl,
        ttlMs,
        now,
      });
      service = new EpisodeService({ repositories, tokenSigner: signer, now });
      registerAckRoutes(app, { episodeService: service });
    },
  });
  return {
    harness,
    service,
    signer,
    advanceTo(date) {
      current = date;
    },
  };
}

function pathOf(url) {
  return new URL(url).pathname;
}

function openEpisode(service, overrides = {}) {
  return service.openEpisode({
    embassyName: 'Ambassade de France',
    embassyPageUrl: 'https://example.test/ambassade',
    excerpt: 'Le contenu de la page a changé.',
    rules: [
      { ruleKey: 'visa', label: 'Section visa modifiée', detail: 'texte ajouté' },
      { ruleKey: 'contact', label: 'Contact modifié' },
    ],
    ...overrides,
  });
}

test('the confirmation page is public and does not mutate the episode', async (t) => {
  const { harness, service } = await setup(t);
  const opened = openEpisode(service);
  const path = pathOf(opened.acknowledgementUrl);

  const response = await fetch(`${harness.baseUrl}${path}`);
  assert.equal(response.status, 200);
  assert.match(response.headers.get('content-type'), /text\/html/);
  const body = await response.text();
  assert.match(body, /Acquitter l’alerte/);
  assert.match(body, /Ambassade de France/);
  assert.match(body, /Section visa modifiée/);
  assert.match(body, /Contact modifié/);
  assert.match(body, /Le contenu de la page a changé/);
  assert.match(body, /https:\/\/example\.test\/ambassade/);
  assert.match(body, /method="post"/);
  assert.match(body, /L’ouverture de cette page n’a rien modifié/);

  for (let index = 0; index < 3; index += 1) {
    const again = await fetch(`${harness.baseUrl}${path}`);
    assert.equal(again.status, 200);
  }
  const stored = harness.repositories.episodes.getById(opened.episode.id);
  assert.equal(stored.status, 'open');
  assert.equal(stored.acknowledgedAt, null);
});

test('user content is HTML-escaped on the confirmation page', async (t) => {
  const { harness, service } = await setup(t);
  const opened = openEpisode(service, {
    embassyName: '<b>Ambassade</b>',
    excerpt: '<script>alert("x")</script>',
    rules: [{ ruleKey: 'xss', label: '<img src=x onerror=alert(1)>' }],
  });
  const response = await fetch(`${harness.baseUrl}${pathOf(opened.acknowledgementUrl)}`);
  const body = await response.text();

  assert.equal(response.status, 200);
  assert.doesNotMatch(body, /<script>alert/);
  assert.doesNotMatch(body, /<img src=x/);
  assert.match(body, /&lt;script&gt;alert\(&quot;x&quot;\)&lt;\/script&gt;/);
  assert.match(body, /&lt;b&gt;Ambassade&lt;\/b&gt;/);
});

test('POST acknowledges without Basic auth or a CSRF token and is single-use', async (t) => {
  const { harness, service } = await setup(t);
  const opened = openEpisode(service);
  const path = pathOf(opened.acknowledgementUrl);

  const response = await fetch(`${harness.baseUrl}${path}`, { method: 'POST' });
  assert.equal(response.status, 200);
  const body = await response.text();
  assert.match(body, /Épisode acquitté/);
  assert.match(body, /2026-11-04 10:00 UTC/);

  const stored = harness.repositories.episodes.getById(opened.episode.id);
  assert.equal(stored.status, 'acknowledged');
  assert.equal(stored.acknowledgedAt, NOW.toISOString());

  const replay = await fetch(`${harness.baseUrl}${path}`, { method: 'POST' });
  assert.equal(replay.status, 409);
  assert.match(await replay.text(), /déjà été acquitté/);
  assert.equal(
    harness.repositories.episodes.getById(opened.episode.id).acknowledgedAt,
    stored.acknowledgedAt,
  );

  const previewAfter = await fetch(`${harness.baseUrl}${path}`);
  assert.equal(previewAfter.status, 409);
  assert.equal(harness.repositories.episodes.getById(opened.episode.id).status, 'acknowledged');
});

test('tampered, malformed and unknown tokens return error pages without mutation', async (t) => {
  const { harness, service } = await setup(t);
  const opened = openEpisode(service);
  const path = pathOf(opened.acknowledgementUrl);
  const tampered = `${path.slice(0, -1)}${path.endsWith('A') ? 'B' : 'A'}`;

  const tamperedResponse = await fetch(`${harness.baseUrl}${tampered}`);
  assert.equal(tamperedResponse.status, 400);
  assert.match(await tamperedResponse.text(), /signature/);

  const malformedResponse = await fetch(`${harness.baseUrl}/ack/not-a-token`);
  assert.equal(malformedResponse.status, 400);
  assert.match(await malformedResponse.text(), /incomplet ou mal formé/);

  const unknownToken = service.tokenSigner.issue({
    episodeId: 999,
    nonce: 'unknown',
    expiresAt: service.tokenSigner.nextExpiry(),
  });
  const unknownResponse = await fetch(
    `${harness.baseUrl}${pathOf(service.tokenSigner.acknowledgementUrl(unknownToken))}`,
  );
  assert.equal(unknownResponse.status, 404);
  assert.match(await unknownResponse.text(), /Aucun épisode/);

  assert.equal(harness.repositories.episodes.getById(opened.episode.id).status, 'open');
});

test('a missing token returns a 400 error page for GET and POST', async (t) => {
  const { harness } = await setup(t);
  for (const path of ['/ack', '/ack/']) {
    const response = await fetch(`${harness.baseUrl}${path}`);
    assert.equal(response.status, 400);
    assert.match(await response.text(), /aucun jeton/);
  }
  const post = await fetch(`${harness.baseUrl}/ack`, { method: 'POST' });
  assert.equal(post.status, 400);
  assert.match(await post.text(), /aucun jeton/);
});

test('an expired link returns 410 and changes nothing', async (t) => {
  const { harness, service, advanceTo } = await setup(t, { ttlMs: 1000 });
  const opened = openEpisode(service);
  const path = pathOf(opened.acknowledgementUrl);

  advanceTo(new Date(NOW.getTime() + 1000));
  const response = await fetch(`${harness.baseUrl}${path}`);
  assert.equal(response.status, 410);
  assert.match(await response.text(), /expiré/);

  const post = await fetch(`${harness.baseUrl}${path}`, { method: 'POST' });
  assert.equal(post.status, 410);
  assert.equal(harness.repositories.episodes.getById(opened.episode.id).status, 'open');
});
