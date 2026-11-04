import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { createResendClient } from '../src/email/resend-client.js';
import { createService } from '../src/server.js';
import {
  baseTestEnv,
  basicAuthorization,
  csrfFromResponse,
  TEST_OPERATOR_PASSWORD,
  TEST_OPERATOR_USERNAME,
} from './helpers.js';

/**
 * Offline end-to-end acceptance tests.
 *
 * These tests run the real HTTP server, a temporary SQLite database and the real
 * Resend HTTP client. Only `fetch` is replaced, so the request shape, headers,
 * status handling and persisted state are exercised without contacting Resend.
 */

const SETTINGS = Object.freeze({
  sender: 'alertes@alertes.vlanp.com',
  recipient: 'operateur@example.test',
  sendingSubdomain: 'alertes',
});
const API_KEY = 're_acceptance_test_key_1234567890';
const INVALID_KEY = 're_acceptance_invalid_key_000000';
const PUBLIC_BASE_URL = 'https://alertes.example.test';

const FORBIDDEN_CLAIMS = Object.freeze([
  /délivrab/i,
  /taux d[’']ouverture/i,
  /open rate/i,
  /\bclics?\b/i,
  /\bclicks?\b/i,
  /\btracking\b/i,
  /\bwebhook\b/i,
  /suivi (?:des? )?(?:ouvertures?|clics?)/i,
]);

function createFakeResend() {
  const requests = [];
  let responder = () => ({
    status: 200,
    body: { id: `resend-accepted-${requests.length}` },
  });

  async function fetchImpl(url, init = {}) {
    const headers = {};
    for (const [name, value] of Object.entries(init.headers ?? {})) {
      headers[name.toLowerCase()] = value;
    }
    let body = null;
    try {
      body = typeof init.body === 'string' ? JSON.parse(init.body) : null;
    } catch {
      body = null;
    }
    const request = {
      url: String(url),
      method: init.method ?? 'GET',
      headers,
      body,
    };
    requests.push(request);
    const result = responder(request, requests.length);
    return new Response(JSON.stringify(result.body ?? {}), {
      status: result.status,
      headers: { 'content-type': 'application/json' },
    });
  }

  return {
    requests,
    fetchImpl,
    accept(providerMessageId = 'resend-accepted') {
      responder = () => ({ status: 200, body: { id: providerMessageId } });
    },
    refuse({
      status = 401,
      name = 'validation_error',
      message = 'API key is invalid',
    } = {}) {
      responder = () => ({ status, body: { statusCode: status, name, message } });
    },
  };
}

async function createAcceptanceHarness(t, { env = {} } = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'ambassade-alerts-acceptance-'));
  const resend = createFakeResend();
  const context = {
    dir,
    resend,
    service: null,
    baseUrl: null,
    authorization: basicAuthorization(TEST_OPERATOR_USERNAME, TEST_OPERATOR_PASSWORD),
  };

  async function start() {
    context.service = createService({
      env: baseTestEnv(dir, {
        LOG_LEVEL: 'silent',
        PUBLIC_BASE_URL,
        ...env,
      }),
      resendClient: createResendClient({ fetchImpl: resend.fetchImpl }),
    });
    const address = await context.service.listen();
    context.baseUrl = `http://127.0.0.1:${address.port}`;
  }

  context.restart = async () => {
    await context.service.close();
    await start();
  };
  context.close = async () => {
    if (context.service !== null) {
      await context.service.close();
      context.service = null;
    }
  };

  t.after(async () => {
    await context.close();
    rmSync(dir, { recursive: true, force: true });
  });

  await start();
  return context;
}

function getPage(context, path, { authorization = true } = {}) {
  const headers = {};
  if (authorization) {
    headers.authorization = context.authorization;
  }
  return fetch(`${context.baseUrl}${path}`, { headers, redirect: 'manual' });
}

async function csrfFor(context, path = '/') {
  const response = await getPage(context, path);
  assert.equal(response.status, 200);
  const csrf = csrfFromResponse(response);
  assert.ok(csrf, 'a CSRF cookie is issued on safe requests');
  return csrf;
}

async function postForm(context, path, fields, { csrf = null } = {}) {
  const body = new URLSearchParams();
  for (const [name, value] of Object.entries(fields)) {
    body.set(name, value);
  }
  const headers = {
    'content-type': 'application/x-www-form-urlencoded',
    authorization: context.authorization,
  };
  if (csrf !== null) {
    body.set('_csrf', csrf.token);
    headers.cookie = csrf.cookie;
  }
  return fetch(`${context.baseUrl}${path}`, {
    method: 'POST',
    redirect: 'manual',
    headers,
    body: body.toString(),
  });
}

async function saveSettings(context, overrides = {}) {
  const csrf = await csrfFor(context, '/settings');
  const response = await postForm(
    context,
    '/settings',
    { ...SETTINGS, apiKey: API_KEY, ...overrides },
    { csrf },
  );
  assert.equal(response.status, 303, response.status === 303 ? '' : await response.text());
  return response;
}

async function postTestEmail(context) {
  const csrf = await csrfFor(context, '/');
  return postForm(context, '/email/test', {}, { csrf });
}

async function openEpisode(context, overrides = {}) {
  const csrf = await csrfFor(context, '/episodes/new');
  return postForm(
    context,
    '/episodes',
    {
      embassyName: 'Ambassade de France au Test',
      embassyPageUrl: 'https://ambassade.example.test/visas',
      excerpt: 'Le contenu a changé : <nouveau> & important.',
      contentHash: 'hash-acceptance',
      rule0Key: 'visa',
      rule0Label: 'Section visa modifiée',
      rule0Detail: 'Les délais de rendez-vous ont changé.',
      rule1Key: 'contact',
      rule1Label: 'Bloc contact modifié',
      rule1Detail: 'Le numéro de téléphone a changé.',
      ...overrides,
    },
    { csrf },
  );
}

function attemptsOf(context) {
  return context.service.repositories.emailAttempts.list();
}

function acknowledgementLink(messageText) {
  return messageText.match(/Acquitter cet épisode : (\S+)/)?.[1] ?? null;
}

test('two rules at one control produce one episode attempt and one provider request', async (t) => {
  const context = await createAcceptanceHarness(t);
  await saveSettings(context);
  context.resend.accept('resend-accepted-episode-1');

  const response = await openEpisode(context);
  assert.equal(response.status, 303);
  assert.equal(response.headers.get('location'), '/?opened=1&delivery=accepted');

  assert.equal(context.resend.requests.length, 1, 'exactly one provider request');
  assert.equal(attemptsOf(context).length, 1, 'exactly one persisted attempt');

  const [request] = context.resend.requests;
  assert.equal(request.method, 'POST');
  assert.equal(request.url, 'https://api.resend.com/emails');
  assert.equal(request.headers.authorization, `Bearer ${API_KEY}`);
  assert.equal(request.headers['idempotency-key'], 'episode-1');
  assert.equal(request.headers['content-type'], 'application/json');

  const message = request.body;
  assert.equal(message.from, SETTINGS.sender);
  assert.equal(message.to, SETTINGS.recipient);
  assert.match(message.subject, /Alerte ambassade/);
  assert.match(message.subject, /Ambassade de France au Test/);

  for (const part of [message.html, message.text]) {
    assert.match(part, /Section visa modifiée/);
    assert.match(part, /Bloc contact modifié/);
    assert.match(part, /Les délais de rendez-vous ont changé\./);
    assert.match(part, /Le numéro de téléphone a changé\./);
    assert.match(part, /https:\/\/ambassade\.example\.test\/visas/);
    assert.ok(
      part.includes(`${PUBLIC_BASE_URL}/ack/`),
      'the message contains an absolute acknowledgement link',
    );
  }
  assert.match(message.text, /Le contenu a changé : <nouveau> & important\./);
  assert.match(
    message.html,
    /Le contenu a changé : &lt;nouveau&gt; &amp; important\./,
    'the excerpt is HTML-escaped',
  );

  const [attempt] = attemptsOf(context);
  assert.equal(attempt.kind, 'episode');
  assert.equal(attempt.episodeId, 1);
  assert.equal(attempt.status, 'accepted');
  assert.equal(attempt.providerMessageId, 'resend-accepted-episode-1');
  assert.equal(attempt.reason, null);

  const stored = context.service.repositories.episodes.getWithRules(1);
  assert.equal(stored.status, 'open');
  assert.equal(stored.rules.length, 2);

  const link = acknowledgementLink(message.text);
  assert.ok(link, 'the message contains a signed acknowledgement link');
  assert.equal(new URL(link).origin, PUBLIC_BASE_URL);
  const path = new URL(link).pathname;

  const confirmation = await getPage(context, path, { authorization: false });
  assert.equal(confirmation.status, 200);
  assert.match(await confirmation.text(), /Acquitter l’alerte/);

  const acknowledged = await fetch(`${context.baseUrl}${path}`, {
    method: 'POST',
    redirect: 'manual',
  });
  assert.equal(acknowledged.status, 200);
  assert.match(await acknowledged.text(), /Épisode acquitté/);
  assert.equal(context.service.repositories.episodes.getWithRules(1).status, 'acknowledged');

  const replay = await getPage(context, path, { authorization: false });
  assert.equal(replay.status, 409);
  assert.equal(context.resend.requests.length, 1);
});

test('an invalid provider key is refused with the provider message visible, and repeated refusals raise the banner', async (t) => {
  const context = await createAcceptanceHarness(t);
  await saveSettings(context, { apiKey: INVALID_KEY });
  context.resend.refuse({
    status: 401,
    name: 'validation_error',
    message: 'API key is invalid',
  });

  const first = await openEpisode(context);
  assert.equal(first.status, 303);
  assert.equal(first.headers.get('location'), '/?opened=1&delivery=refused');
  assert.equal(context.resend.requests.length, 1);

  const [firstAttempt] = attemptsOf(context);
  assert.equal(firstAttempt.status, 'refused');
  assert.equal(firstAttempt.providerMessageId, null);
  assert.match(firstAttempt.reason, /HTTP 401/);
  assert.match(firstAttempt.reason, /validation_error/);
  assert.match(firstAttempt.reason, /API key is invalid/);
  assert.equal(
    firstAttempt.reason.includes(INVALID_KEY),
    false,
    'the provider reason never contains the API key',
  );

  const firstDashboard = await (await getPage(context, '/?opened=1&delivery=refused')).text();
  assert.match(firstDashboard, /Refusé/);
  assert.match(firstDashboard, /API key is invalid/);
  assert.doesNotMatch(
    firstDashboard,
    /Échecs d’envoi répétés/,
    'one refusal does not raise the banner',
  );

  const second = await openEpisode(context);
  assert.equal(second.status, 303);
  assert.equal(second.headers.get('location'), '/?opened=2&delivery=refused');
  assert.equal(context.resend.requests.length, 2);
  assert.equal(attemptsOf(context).length, 2);
  assert.ok(attemptsOf(context).every((attempt) => attempt.status === 'refused'));

  const secondDashboard = await (await getPage(context, '/?opened=2&delivery=refused')).text();
  assert.match(secondDashboard, /Échecs d’envoi répétés/);
  assert.match(secondDashboard, /Vérifier la configuration email/);
  assert.match(secondDashboard, /API key is invalid/);

  const health = await fetch(`${context.baseUrl}/health`);
  assert.equal(health.status, 200);
  assert.deepEqual(await health.json(), { status: 'ok', database: 'ok' });
  const dashboard = await getPage(context, '/');
  assert.equal(dashboard.status, 200, 'the service stays responsive after refusals');
});

test('the banner starts at the second refusal, ignores not_attempted, survives a reopen and clears on acceptance', async (t) => {
  const context = await createAcceptanceHarness(t);
  await saveSettings(context);
  context.resend.refuse({
    status: 403,
    name: 'validation_error',
    message: 'Domain is not verified',
  });

  const first = await postTestEmail(context);
  assert.equal(first.headers.get('location'), '/?test=refused');
  let body = await (await getPage(context, '/')).text();
  assert.doesNotMatch(body, /Échecs d’envoi répétés/);

  const second = await postTestEmail(context);
  assert.equal(second.headers.get('location'), '/?test=refused');
  assert.equal(context.resend.requests.length, 2);
  body = await (await getPage(context, '/')).text();
  assert.match(body, /Échecs d’envoi répétés/);
  assert.match(body, /Domain is not verified/);

  rmSync(join(context.dir, 'resend-api-key'));
  const notAttempted = await postTestEmail(context);
  assert.equal(notAttempted.headers.get('location'), '/?test=not_attempted');
  assert.equal(context.resend.requests.length, 2, 'not_attempted never calls the provider');
  assert.equal(attemptsOf(context).length, 3);
  assert.equal(
    attemptsOf(context).filter((attempt) => attempt.status === 'refused').length,
    2,
  );
  assert.equal(
    attemptsOf(context).filter((attempt) => attempt.status === 'not_attempted').length,
    1,
  );
  body = await (await getPage(context, '/')).text();
  assert.match(
    body,
    /Échecs d’envoi répétés/,
    'not_attempted is ignored by the banner state',
  );

  await context.restart();
  body = await (await getPage(context, '/')).text();
  assert.match(body, /Échecs d’envoi répétés/, 'the banner survives a database reopen');
  assert.match(body, /Refusé/);
  assert.equal(attemptsOf(context).length, 3);

  await saveSettings(context, { apiKey: API_KEY });
  context.resend.accept('resend-banner-cleared');
  const accepted = await postTestEmail(context);
  assert.equal(accepted.headers.get('location'), '/?test=accepted');
  body = await (await getPage(context, '/')).text();
  assert.doesNotMatch(body, /Échecs d’envoi répétés/, 'an acceptance clears the banner');
  assert.match(body, /Accepté par le fournisseur/);
});

test('missing configuration records not_attempted without a provider request', async (t) => {
  const context = await createAcceptanceHarness(t);

  const response = await openEpisode(context);
  assert.equal(response.status, 303);
  assert.equal(response.headers.get('location'), '/?opened=1&delivery=not_attempted');
  assert.equal(context.resend.requests.length, 0, 'no provider request without configuration');

  const [attempt] = attemptsOf(context);
  assert.equal(attempt.kind, 'episode');
  assert.equal(attempt.status, 'not_attempted');
  assert.match(attempt.reason, /Configuration email incomplète/);
  assert.match(attempt.reason, /expéditeur/);
  assert.match(attempt.reason, /destinataire/);
  assert.match(attempt.reason, /sous-domaine d’envoi/);
  assert.match(attempt.reason, /clé API Resend/);

  const body = await (await getPage(context, '/?opened=1&delivery=not_attempted')).text();
  assert.match(body, /Non tenté/);
  assert.match(body, /Configuration email incomplète/);
  assert.match(body, /Envoi : non tenté/);

  const health = await fetch(`${context.baseUrl}/health`);
  assert.equal(health.status, 200);
});

test('the settings form saves the configuration, writes a 0600 key file and masks the key', async (t) => {
  const context = await createAcceptanceHarness(t);

  const initial = await (await getPage(context, '/settings')).text();
  assert.match(initial, /Configuration email incomplète/);
  assert.match(initial, /id="apiKey"[^>]*value=""/);

  const saved = await saveSettings(context);
  assert.equal(saved.status, 303);
  assert.deepEqual(context.service.emailSettings.getSettings(), {
    sender: SETTINGS.sender,
    recipient: SETTINGS.recipient,
    sendingSubdomain: SETTINGS.sendingSubdomain,
  });

  const keyPath = join(context.dir, 'resend-api-key');
  assert.equal(statSync(keyPath).mode & 0o777, 0o600, 'the key file is private');
  assert.equal(readFileSync(keyPath, 'utf8').trim(), API_KEY);

  const configured = await (await getPage(context, '/settings')).text();
  assert.match(configured, /Configuration email complète/);
  assert.match(configured, /Une clé est enregistrée/);
  assert.ok(configured.includes(`value="${SETTINGS.sender}"`));
  assert.ok(configured.includes(`value="${SETTINGS.recipient}"`));
  assert.ok(configured.includes(`value="${SETTINGS.sendingSubdomain}"`));
  assert.equal(configured.includes(API_KEY), false, 'the API key is never re-displayed');
  assert.equal(configured.includes('re_acceptance_test'), false);
  assert.match(configured, /id="apiKey"[^>]*value=""/);

  const csrf = await csrfFor(context, '/settings');
  const invalid = await postForm(
    context,
    '/settings',
    { recipient: 'destinataire-invalide' },
    { csrf },
  );
  assert.equal(invalid.status, 422);
  const invalidBody = await invalid.text();
  assert.match(invalidBody, /adresse email unique et valide/);
  assert.equal(invalidBody.includes(API_KEY), false);
  assert.equal(context.service.emailSettings.getSettings().recipient, SETTINGS.recipient);

  const health = await fetch(`${context.baseUrl}/health`);
  assert.equal(health.status, 200);
});

test('a tampered acknowledgement link changes nothing and the genuine link still works', async (t) => {
  const context = await createAcceptanceHarness(t);
  await saveSettings(context);
  context.resend.accept('resend-ack-tamper');

  const response = await openEpisode(context);
  assert.equal(response.status, 303);
  assert.equal(context.resend.requests.length, 1);

  const [request] = context.resend.requests;
  const link = acknowledgementLink(request.body.text);
  assert.ok(link);
  const token = new URL(link).pathname.split('/').pop();
  const [payload, signature] = token.split('.');
  const tamperedSignature = `${signature[0] === 'A' ? 'B' : 'A'}${signature.slice(1)}`;
  const tamperedPath = `/ack/${payload}.${tamperedSignature}`;

  const tamperedGet = await getPage(context, tamperedPath, { authorization: false });
  assert.equal(tamperedGet.status, 400);
  assert.match(
    await tamperedGet.text(),
    /signature de ce lien d’acquittement est invalide|incomplet ou mal formé/,
  );

  const tamperedPost = await fetch(`${context.baseUrl}${tamperedPath}`, {
    method: 'POST',
    redirect: 'manual',
  });
  assert.equal(tamperedPost.status, 400);

  const unchanged = context.service.repositories.episodes.getWithRules(1);
  assert.equal(unchanged.status, 'open', 'a tampered link changes nothing');
  assert.equal(attemptsOf(context).length, 1);
  assert.equal(context.resend.requests.length, 1);

  const genuinePath = `/ack/${token}`;
  const genuine = await getPage(context, genuinePath, { authorization: false });
  assert.equal(genuine.status, 200);
  const confirmed = await fetch(`${context.baseUrl}${genuinePath}`, {
    method: 'POST',
    redirect: 'manual',
  });
  assert.equal(confirmed.status, 200);
  assert.equal(context.service.repositories.episodes.getWithRules(1).status, 'acknowledged');
});

test('the interface and emails make no deliverability, opening or click claim', async (t) => {
  const context = await createAcceptanceHarness(t);
  await saveSettings(context);
  context.resend.accept('resend-wording');

  const response = await openEpisode(context);
  assert.equal(response.status, 303);
  await postTestEmail(context);
  assert.equal(context.resend.requests.length, 2);

  const pages = [];
  for (const path of ['/?opened=1&delivery=accepted', '/settings', '/episodes/new']) {
    const page = await getPage(context, path);
    assert.equal(page.status, 200);
    pages.push(await page.text());
  }

  const [alertEmail, testEmail] = context.resend.requests.map((request) => request.body);
  const ackPath = new URL(acknowledgementLink(alertEmail.text)).pathname;
  const acknowledgementPage = await getPage(context, ackPath, { authorization: false });
  assert.equal(acknowledgementPage.status, 200);

  const contents = [
    ...pages,
    await acknowledgementPage.text(),
    alertEmail.subject,
    alertEmail.html,
    alertEmail.text,
    testEmail.subject,
    testEmail.html,
    testEmail.text,
  ];
  for (const content of contents) {
    for (const pattern of FORBIDDEN_CLAIMS) {
      assert.doesNotMatch(content, pattern);
    }
  }

  assert.match(pages[0], /Accepté par le fournisseur/);
  assert.match(alertEmail.text, /Règles déclenchées/);
  assert.match(testEmail.text, /Test de configuration/);
});
