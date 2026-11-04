import assert from 'node:assert/strict';
import test from 'node:test';
import { createTestHarness, csrfFromResponse } from './helpers.js';

function registerProbeRoutes(app) {
  app.get('/private', (ctx) => {
    ctx.sendJson(200, { ok: true, csrfToken: ctx.csrfToken });
  });
  app.post('/echo', (ctx) => {
    ctx.sendJson(200, { form: ctx.form ?? null, json: ctx.json ?? null, text: ctx.text ?? null });
  });
  app.get('/boom', () => {
    throw new Error('internal detail that must not leak');
  });
  app.post('/public-hook', (ctx) => ctx.sendJson(200, { public: true }), {
    public: true,
    csrf: false,
  });
}

test('health is public and applies security headers', async (t) => {
  const harness = await createTestHarness(t, { registerRoutes: registerProbeRoutes });
  const response = await fetch(`${harness.baseUrl}/health`);

  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { status: 'ok', database: 'ok' });
  assert.equal(response.headers.get('x-content-type-options'), 'nosniff');
  assert.equal(response.headers.get('x-frame-options'), 'DENY');
  assert.equal(response.headers.get('referrer-policy'), 'no-referrer');
  assert.match(response.headers.get('content-security-policy'), /default-src 'self'/);
  assert.equal(response.headers.get('cache-control'), 'no-store');

  const csrf = csrfFromResponse(response);
  assert.ok(csrf);
  assert.match(csrf.token, /^[a-f0-9]{64}$/);

  const head = await fetch(`${harness.baseUrl}/health`, { method: 'HEAD' });
  assert.equal(head.status, 200);
  assert.equal(await head.text(), '');
});

test('protected routes require correct Basic credentials', async (t) => {
  const harness = await createTestHarness(t, { registerRoutes: registerProbeRoutes });

  const missing = await fetch(`${harness.baseUrl}/private`);
  assert.equal(missing.status, 401);
  assert.match(missing.headers.get('www-authenticate'), /^Basic /);
  assert.match(await missing.text(), /Authentification requise/);

  const wrong = await fetch(`${harness.baseUrl}/private`, {
    headers: { authorization: `Basic ${Buffer.from('operator:wrong').toString('base64')}` },
  });
  assert.equal(wrong.status, 401);

  const authorized = await fetch(`${harness.baseUrl}/private`, {
    headers: { authorization: harness.operatorAuthorization },
  });
  assert.equal(authorized.status, 200);
  const body = await authorized.json();
  assert.equal(body.ok, true);
  const csrf = csrfFromResponse(authorized);
  assert.equal(body.csrfToken, csrf.token);
});

test('unknown routes require authentication and then return 404', async (t) => {
  const harness = await createTestHarness(t, { registerRoutes: registerProbeRoutes });

  const unauthenticated = await fetch(`${harness.baseUrl}/does-not-exist`);
  assert.equal(unauthenticated.status, 401);

  const authenticated = await fetch(`${harness.baseUrl}/does-not-exist`, {
    headers: { authorization: harness.operatorAuthorization },
  });
  assert.equal(authenticated.status, 404);
});

test('state-changing requests require the CSRF cookie and token', async (t) => {
  const harness = await createTestHarness(t, { registerRoutes: registerProbeRoutes });
  const authorization = harness.operatorAuthorization;

  const page = await fetch(`${harness.baseUrl}/private`, { headers: { authorization } });
  const csrf = csrfFromResponse(page);
  assert.ok(csrf);

  const noToken = await fetch(`${harness.baseUrl}/echo`, {
    method: 'POST',
    headers: {
      authorization,
      cookie: csrf.cookie,
      'content-type': 'application/json',
    },
    body: JSON.stringify({ hello: 'world' }),
  });
  assert.equal(noToken.status, 403);

  const noCookie = await fetch(`${harness.baseUrl}/echo`, {
    method: 'POST',
    headers: {
      authorization,
      'x-csrf-token': csrf.token,
      'content-type': 'application/json',
    },
    body: JSON.stringify({ hello: 'world' }),
  });
  assert.equal(noCookie.status, 403);

  const wrongToken = await fetch(`${harness.baseUrl}/echo`, {
    method: 'POST',
    headers: {
      authorization,
      cookie: csrf.cookie,
      'x-csrf-token': 'a'.repeat(64),
      'content-type': 'application/json',
    },
    body: JSON.stringify({ hello: 'world' }),
  });
  assert.equal(wrongToken.status, 403);

  const headerToken = await fetch(`${harness.baseUrl}/echo`, {
    method: 'POST',
    headers: {
      authorization,
      cookie: csrf.cookie,
      'x-csrf-token': csrf.token,
      'content-type': 'application/json',
    },
    body: JSON.stringify({ hello: 'world' }),
  });
  assert.equal(headerToken.status, 200);
  assert.deepEqual(await headerToken.json(), { form: null, json: { hello: 'world' }, text: null });

  const formToken = await fetch(`${harness.baseUrl}/echo`, {
    method: 'POST',
    headers: {
      authorization,
      cookie: csrf.cookie,
      'content-type': 'application/x-www-form-urlencoded',
    },
    body: new URLSearchParams({ _csrf: csrf.token, name: 'valeur' }).toString(),
  });
  assert.equal(formToken.status, 200);
  const formBody = await formToken.json();
  assert.equal(formBody.form.name, 'valeur');
  assert.equal(formBody.form._csrf, csrf.token);
});

test('routes can opt out of auth and CSRF for signed public links', async (t) => {
  const harness = await createTestHarness(t, { registerRoutes: registerProbeRoutes });
  const response = await fetch(`${harness.baseUrl}/public-hook`, { method: 'POST' });
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { public: true });
});

test('request bodies over the configured limit are rejected with 413', async (t) => {
  const harness = await createTestHarness(t, {
    env: { MAX_BODY_BYTES: '1024' },
    registerRoutes: registerProbeRoutes,
  });
  const response = await fetch(`${harness.baseUrl}/echo`, {
    method: 'POST',
    headers: {
      authorization: harness.operatorAuthorization,
      'content-type': 'text/plain',
    },
    body: 'x'.repeat(2048),
  });
  assert.equal(response.status, 413);
  assert.match(await response.text(), /trop volumineux/);
});

test('malformed JSON is rejected as a bad request', async (t) => {
  const harness = await createTestHarness(t, { registerRoutes: registerProbeRoutes });
  const response = await fetch(`${harness.baseUrl}/echo`, {
    method: 'POST',
    headers: {
      authorization: harness.operatorAuthorization,
      'content-type': 'application/json',
    },
    body: '{not json',
  });
  assert.equal(response.status, 400);
  assert.match(await response.text(), /JSON invalide/);
});

test('unexpected errors return a generic 500 without internal details', async (t) => {
  const harness = await createTestHarness(t, { registerRoutes: registerProbeRoutes });
  const response = await fetch(`${harness.baseUrl}/boom`, {
    headers: { authorization: harness.operatorAuthorization },
  });
  assert.equal(response.status, 500);
  const body = await response.text();
  assert.equal(body, 'Erreur interne du serveur.');
  assert.doesNotMatch(body, /internal detail/);
  assert.match(harness.lines.join(''), /http_request_failed/);
});
