import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { createLogger } from '../src/logger.js';
import { createService } from '../src/server.js';
import { baseTestEnv, csrfFromResponse, TEST_OPERATOR_PASSWORD, TEST_OPERATOR_USERNAME } from './helpers.js';

const API_KEY = 're_ui_test_secret_key_1234567890';
const SETTINGS = Object.freeze({
  sender: 'alertes@alertes.vlanp.com',
  recipient: 'operateur@example.test',
  sendingSubdomain: 'alertes',
  apiKey: API_KEY,
});

function accepted(providerMessageId = 'resend-ui-accepted-1') {
  return { status: 'accepted', providerMessageId, reason: null };
}

function refused(reason = 'HTTP 401 (validation_error) : clé API invalide') {
  return { status: 'refused', providerMessageId: null, reason };
}

function createFakeClient(handler = () => accepted()) {
  const calls = [];
  return {
    calls,
    async send(options) {
      calls.push(options);
      return handler(options, calls.length);
    },
  };
}

async function setup(t, { handler } = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'ambassade-alerts-ui-'));
  const lines = [];
  const stream = {
    write(chunk) {
      lines.push(String(chunk));
      return true;
    },
  };
  const logger = createLogger({ level: 'debug', stream, errorStream: stream });
  const client = createFakeClient(handler);
  const service = createService({
    env: baseTestEnv(dir, { LOG_LEVEL: 'silent' }),
    logger,
    resendClient: client,
  });
  t.after(async () => {
    await service.close();
    rmSync(dir, { recursive: true, force: true });
  });
  const address = await service.listen();
  return {
    service,
    client,
    lines,
    dir,
    baseUrl: `http://127.0.0.1:${address.port}`,
    authorization: `Basic ${Buffer.from(
      `${TEST_OPERATOR_USERNAME}:${TEST_OPERATOR_PASSWORD}`,
      'utf8',
    ).toString('base64')}`,
  };
}

async function getPage(context, path, { authorization = true } = {}) {
  return fetch(`${context.baseUrl}${path}`, {
    headers: authorization ? { authorization: context.authorization } : {},
  });
}

async function csrfFor(context, path = '/') {
  const response = await getPage(context, path);
  assert.equal(response.status, 200);
  const csrf = csrfFromResponse(response);
  assert.ok(csrf, 'a CSRF cookie is issued on safe requests');
  return csrf;
}

async function postForm(context, path, fields, { csrf = null, authorization = true } = {}) {
  const body = new URLSearchParams();
  for (const [name, value] of Object.entries(fields)) {
    body.set(name, value);
  }
  if (csrf !== null) {
    body.set('_csrf', csrf.token);
  }
  const headers = { 'content-type': 'application/x-www-form-urlencoded' };
  if (authorization) {
    headers.authorization = context.authorization;
  }
  if (csrf !== null) {
    headers.cookie = csrf.cookie;
  }
  return fetch(`${context.baseUrl}${path}`, {
    method: 'POST',
    redirect: 'manual',
    headers,
    body: body.toString(),
  });
}

function assertNoForbiddenClaims(html) {
  assert.doesNotMatch(html, /délivrab/i);
  assert.doesNotMatch(html, /taux d’ouverture/i);
  assert.doesNotMatch(html, /open rate/i);
  assert.doesNotMatch(html, /\bclics?\b/i);
  assert.doesNotMatch(html, /\btracking\b/i);
  assert.doesNotMatch(html, /\bwebhook\b/i);
}

test('the operator interface requires Basic auth and a CSRF token for writes', async (t) => {
  const context = await setup(t);

  const unauthenticated = await fetch(`${context.baseUrl}/`);
  assert.equal(unauthenticated.status, 401);
  assert.match(unauthenticated.headers.get('www-authenticate'), /^Basic /);

  const dashboard = await getPage(context, '/');
  assert.equal(dashboard.status, 200);
  assert.match(dashboard.headers.get('content-type'), /text\/html/);
  assert.match(await dashboard.text(), /Tableau de bord/);

  const noToken = await postForm(context, '/settings', { sender: SETTINGS.sender });
  assert.equal(noToken.status, 403);
  assert.match(await noToken.text(), /CSRF/);

  const csrf = await csrfFor(context, '/');
  const wrongToken = await postForm(
    context,
    '/settings',
    { sender: SETTINGS.sender },
    { csrf: { cookie: csrf.cookie, token: 'a'.repeat(64) } },
  );
  assert.equal(wrongToken.status, 403);
});

test('settings report missing fields, save, and never render the API key', async (t) => {
  const context = await setup(t);

  const initial = await getPage(context, '/settings');
  const initialBody = await initial.text();
  assert.match(initialBody, /Configuration email incomplète/);
  assert.match(initialBody, /expéditeur/);
  assert.match(initialBody, /destinataire/);
  assert.match(initialBody, /sous-domaine d’envoi/);
  assert.match(initialBody, /clé API Resend/);
  assert.match(initialBody, /id="apiKey"[^>]*value=""/);

  const csrf = await csrfFor(context, '/settings');
  const saved = await postForm(context, '/settings', SETTINGS, { csrf });
  assert.equal(saved.status, 303);
  assert.equal(saved.headers.get('location'), '/settings?saved=1');

  const configured = await getPage(context, '/settings');
  const configuredBody = await configured.text();
  assert.match(configuredBody, /Configuration email complète/);
  assert.match(configuredBody, /Une clé est enregistrée/);
  assert.match(configuredBody, /id="apiKey"[^>]*value=""/);
  assert.equal(configuredBody.includes(API_KEY), false);
  assert.equal(configuredBody.includes('re_ui_test_secret'), false);

  const dashboard = await getPage(context, '/');
  const dashboardBody = await dashboard.text();
  assert.match(dashboardBody, /Configuration email complète/);
  assert.equal(dashboardBody.includes(API_KEY), false);
  assertNoForbiddenClaims(configuredBody);
  assertNoForbiddenClaims(dashboardBody);
});

test('invalid settings produce a visible French error and leave the service responsive', async (t) => {
  const context = await setup(t);
  const csrf = await csrfFor(context, '/settings');

  const response = await postForm(
    context,
    '/settings',
    {
      sender: SETTINGS.sender,
      recipient: 'destinataire-invalide',
      sendingSubdomain: SETTINGS.sendingSubdomain,
      apiKey: API_KEY,
    },
    { csrf },
  );
  assert.equal(response.status, 422);
  const body = await response.text();
  assert.match(body, /doit être une adresse email unique et valide/);
  assert.equal(body.includes(API_KEY), false);
  assert.equal(context.service.emailSettings.getSettings().recipient, null);

  const mismatchedSender = await postForm(
    context,
    '/settings',
    {
      sender: 'alertes@autre-exemple.test',
      recipient: SETTINGS.recipient,
      sendingSubdomain: SETTINGS.sendingSubdomain,
      apiKey: API_KEY,
    },
    { csrf },
  );
  assert.equal(mismatchedSender.status, 422);
  assert.match(await mismatchedSender.text(), /sous-domaine d’envoi/);
  assert.equal(context.service.emailSettings.getSettings().sender, null);

  const health = await fetch(`${context.baseUrl}/health`);
  assert.equal(health.status, 200);
});

test('the dashboard shows exact persisted statuses with their reasons', async (t) => {
  const context = await setup(t);
  context.service.repositories.emailAttempts.create({
    kind: 'test',
    status: 'accepted',
    providerMessageId: 'provider-message-42',
  });
  context.service.repositories.emailAttempts.create({
    kind: 'test',
    status: 'refused',
    reason: 'HTTP 401 (validation_error) : clé API invalide',
  });
  context.service.repositories.emailAttempts.create({
    kind: 'test',
    status: 'not_attempted',
    reason: 'Configuration email incomplète : clé API Resend.',
  });

  const response = await getPage(context, '/');
  const body = await response.text();
  assert.match(body, /Accepté par le fournisseur/);
  assert.match(body, /Refusé/);
  assert.match(body, /Non tenté/);
  assert.match(body, /provider-message-42/);
  assert.match(body, /HTTP 401 \(validation_error\)/);
  assert.match(body, /Configuration email incomplète : clé API Resend\./);
  assertNoForbiddenClaims(body);
});

test('delivery status comes from persisted rows, never from the query string', async (t) => {
  const context = await setup(t);
  const opened = context.service.episodeService.openEpisode({
    embassyName: 'Ambassade de France',
    embassyPageUrl: 'https://example.test/ambassade',
    excerpt: 'Extrait',
    rules: [{ ruleKey: 'visa', label: 'Section visa modifiée' }],
  });
  context.service.repositories.emailAttempts.create({
    episodeId: opened.episode.id,
    status: 'refused',
    reason: 'HTTP 401 (validation_error) : clé API invalide',
  });

  const forged = await getPage(context, `/?opened=${opened.episode.id}&delivery=accepted`);
  const forgedBody = await forged.text();
  assert.match(forgedBody, /Envoi : refusé/);
  assert.doesNotMatch(forgedBody, /Envoi : accepté/);
  assert.match(forgedBody, /HTTP 401 \(validation_error\)/);

  const pending = context.service.episodeService.openEpisode({
    embassyName: 'Ambassade de France',
    embassyPageUrl: 'https://example.test/autre',
    excerpt: 'Autre extrait',
    rules: [{ ruleKey: 'contact', label: 'Contact modifié' }],
  });
  const pendingBody = await (await getPage(context, `/?opened=${pending.episode.id}`)).text();
  assert.match(pendingBody, /Envoi : état indéterminé/);
  assert.match(pendingBody, /Indéterminé/);
});

test('the banner appears only at two refusals since the last acceptance', async (t) => {
  const context = await setup(t);
  const attempts = context.service.repositories.emailAttempts;

  let body = await (await getPage(context, '/')).text();
  assert.doesNotMatch(body, /Échecs d’envoi répétés/);

  attempts.create({ kind: 'test', status: 'refused', reason: 'HTTP 401.' });
  body = await (await getPage(context, '/')).text();
  assert.doesNotMatch(body, /Échecs d’envoi répétés/);

  attempts.create({ kind: 'test', status: 'refused', reason: 'HTTP 403.' });
  body = await (await getPage(context, '/')).text();
  assert.match(body, /Échecs d’envoi répétés/);
  assert.match(body, /Vérifier la configuration email/);

  attempts.create({
    kind: 'test',
    status: 'not_attempted',
    reason: 'Configuration email incomplète.',
  });
  body = await (await getPage(context, '/')).text();
  assert.match(body, /Échecs d’envoi répétés/);

  attempts.create({
    kind: 'test',
    status: 'accepted',
    providerMessageId: 'provider-clears-banner',
  });
  body = await (await getPage(context, '/')).text();
  assert.doesNotMatch(body, /Échecs d’envoi répétés/);
});

test('two rules submitted once produce a single episode, attempt and provider request', async (t) => {
  const context = await setup(t);
  context.service.emailSettings.update(SETTINGS);

  const csrf = await csrfFor(context, '/episodes/new');
  const response = await postForm(
    context,
    '/episodes',
    {
      embassyName: 'Ambassade de France',
      embassyPageUrl: 'https://example.test/ambassade',
      excerpt: 'Le contenu <modifié> & nouveau.',
      contentHash: 'hash-1',
      rule0Key: 'visa',
      rule0Label: 'Section visa modifiée',
      rule0Detail: 'délai modifié',
      rule1Key: 'contact',
      rule1Label: 'Contact modifié',
      rule1Detail: '',
    },
    { csrf },
  );

  assert.equal(response.status, 303);
  assert.equal(response.headers.get('location'), '/?opened=1');
  assert.equal(context.client.calls.length, 1);

  const attempts = context.service.repositories.emailAttempts.list();
  assert.equal(attempts.length, 1);
  assert.equal(attempts[0].kind, 'episode');
  assert.equal(attempts[0].episodeId, 1);
  assert.equal(attempts[0].status, 'accepted');

  const stored = context.service.repositories.episodes.getWithRules(1);
  assert.equal(stored.status, 'open');
  assert.equal(stored.rules.length, 2);

  const message = context.client.calls[0];
  assert.equal(message.from, SETTINGS.sender);
  assert.equal(message.to, SETTINGS.recipient);
  assert.match(message.subject, /Alerte ambassade/);
  for (const part of [message.html, message.text]) {
    assert.match(part, /Section visa modifiée/);
    assert.match(part, /Contact modifié/);
    assert.match(part, /délai modifié/);
    assert.match(part, /Le contenu/);
    assert.match(part, /https:\/\/example\.test\/ambassade/);
    assert.match(part, /\/ack\//);
  }

  const location = response.headers.get('location');
  const dashboard = await getPage(context, location);
  const body = await dashboard.text();
  assert.match(body, /Épisode n° 1 déclaré/);
  assert.match(body, /Accepté par le fournisseur/);
  assert.match(body, /Section visa modifiée/);
  assert.match(body, /Contact modifié/);
  assert.match(body, /Page d’acquittement/);
  assertNoForbiddenClaims(body);

  const href = body.match(/href="(\/ack\/[^"]+)"/)?.[1];
  assert.ok(href, 'the dashboard contains a signed acknowledgement link');
  const ackPage = await fetch(`${context.baseUrl}${href}`);
  assert.equal(ackPage.status, 200);
  assert.match(await ackPage.text(), /Acquitter l’alerte/);
});

test('the episode form rejects missing rules and invalid URLs without a provider call', async (t) => {
  const context = await setup(t);
  const csrf = await csrfFor(context, '/episodes/new');

  const noRules = await postForm(
    context,
    '/episodes',
    { embassyName: 'Ambassade', embassyPageUrl: 'https://example.test/page' },
    { csrf },
  );
  assert.equal(noRules.status, 400);
  assert.match(await noRules.text(), /au moins une règle/);

  const invalidUrl = await postForm(
    context,
    '/episodes',
    {
      embassyName: 'Ambassade',
      embassyPageUrl: 'javascript:alert(1)',
      rule0Key: 'visa',
      rule0Label: 'Section visa',
    },
    { csrf },
  );
  assert.equal(invalidUrl.status, 400);
  assert.match(await invalidUrl.text(), /URL absolue http\(s\)/);

  const missingUrl = await postForm(
    context,
    '/episodes',
    {
      embassyName: 'Ambassade',
      excerpt: 'Extrait',
      rule0Key: 'visa',
      rule0Label: 'Section visa',
    },
    { csrf },
  );
  assert.equal(missingUrl.status, 400);
  assert.match(await missingUrl.text(), /page de l’ambassade est obligatoire/);

  const missingExcerpt = await postForm(
    context,
    '/episodes',
    {
      embassyName: 'Ambassade',
      embassyPageUrl: 'https://example.test/page',
      excerpt: '   ',
      rule0Key: 'visa',
      rule0Label: 'Section visa',
    },
    { csrf },
  );
  assert.equal(missingExcerpt.status, 400);
  assert.match(await missingExcerpt.text(), /extrait du contenu modifié est obligatoire/);

  assert.equal(context.client.calls.length, 0);
  assert.equal(context.service.repositories.episodes.list().length, 0);
  assert.equal(context.service.repositories.emailAttempts.list().length, 0);
});

test('an unconfigured episode is recorded as not attempted without a provider call', async (t) => {
  const context = await setup(t);
  const csrf = await csrfFor(context, '/episodes/new');

  const response = await postForm(
    context,
    '/episodes',
    {
      embassyName: 'Ambassade de France',
      embassyPageUrl: 'https://example.test/ambassade',
      excerpt: 'Extrait',
      rule0Key: 'visa',
      rule0Label: 'Section visa modifiée',
    },
    { csrf },
  );

  assert.equal(response.status, 303);
  assert.equal(response.headers.get('location'), '/?opened=1');
  assert.equal(context.client.calls.length, 0);
  const attempts = context.service.repositories.emailAttempts.list();
  assert.equal(attempts.length, 1);
  assert.equal(attempts[0].status, 'not_attempted');
  assert.match(attempts[0].reason, /Configuration email incomplète/);

  const body = await (await getPage(context, response.headers.get('location'))).text();
  assert.match(body, /Envoi : non tenté/);
  assert.match(body, /Non tenté/);
  assert.match(body, /Configuration email incomplète/);
  assert.match(body, /clé API Resend/);
});

test('the test-email action records one test attempt and reports its state', async (t) => {
  const context = await setup(t);
  context.service.emailSettings.update(SETTINGS);
  const csrf = await csrfFor(context, '/');

  const response = await postForm(context, '/email/test', {}, { csrf });
  assert.equal(response.status, 303);
  const location = response.headers.get('location');
  assert.equal(context.client.calls.length, 1);

  const attempts = context.service.repositories.emailAttempts.list();
  assert.equal(attempts.length, 1);
  assert.equal(attempts[0].kind, 'test');
  assert.equal(attempts[0].status, 'accepted');
  assert.equal(location, `/?test=${attempts[0].id}`);

  const body = await (await getPage(context, location)).text();
  assert.match(body, /Test d’envoi : accepté par le fournisseur/);
  assert.match(body, /Accepté par le fournisseur/);
});

test('user-entered content is escaped everywhere in the interface', async (t) => {
  const context = await setup(t);
  context.service.emailSettings.update(SETTINGS);
  const csrf = await csrfFor(context, '/episodes/new');

  await postForm(
    context,
    '/episodes',
    {
      embassyName: '<b>Ambassade</b>',
      embassyPageUrl: 'https://example.test/page',
      excerpt: '<script>alert("x")</script>',
      rule0Key: 'xss',
      rule0Label: '<img src=x onerror=alert(1)>',
    },
    { csrf },
  );

  const body = await (await getPage(context, '/')).text();
  assert.doesNotMatch(body, /<script>alert/);
  assert.doesNotMatch(body, /<img src=x/);
  assert.match(body, /&lt;b&gt;Ambassade&lt;\/b&gt;/);
  assert.match(body, /&lt;img src=x onerror=alert\(1\)&gt;/);

  const sent = context.client.calls[0];
  assert.match(sent.html, /&lt;script&gt;alert\(&quot;x&quot;\)&lt;\/script&gt;/);
});
