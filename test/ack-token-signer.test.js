import assert from 'node:assert/strict';
import test from 'node:test';
import {
  ACK_TOKEN_SECRET_NAME,
  AckTokenSigner,
  createAckTokenSigner,
} from '../src/ack/ack-token-signer.js';
import { openTestDatabase } from './helpers.js';

const NOW = new Date('2026-11-04T10:00:00.000Z');
const SECRET = 'ack-test-secret-that-is-long-enough';
const BASE_URL = 'https://alertes.example.test';

function makeSigner({ now = () => NOW, ttlMs = 60_000 } = {}) {
  return new AckTokenSigner({ secret: SECRET, publicBaseUrl: BASE_URL, ttlMs, now });
}

test('tokens round-trip episode id, nonce and expiry and are URL-safe', () => {
  const signer = makeSigner();
  const expiresAt = signer.nextExpiry();
  const token = signer.issue({ episodeId: 42, nonce: 'nonce-abc', expiresAt });

  assert.match(token, /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/);
  assert.deepEqual(signer.verify(token), {
    ok: true,
    episodeId: 42,
    nonce: 'nonce-abc',
    expiresAt,
  });
  assert.equal(
    signer.acknowledgementUrl(token),
    `${BASE_URL}/ack/${encodeURIComponent(token)}`,
  );
});

test('the public base URL must be an origin because acknowledgement routes are at the root', () => {
  for (const publicBaseUrl of [
    'https://alertes.example.test/service',
    'https://alertes.example.test/service/',
    'https://user:pass@alertes.example.test',
    'https://alertes.example.test?x=1',
  ]) {
    assert.throws(
      () => new AckTokenSigner({ secret: SECRET, publicBaseUrl, now: () => NOW }),
      TypeError,
    );
  }
  assert.equal(
    new AckTokenSigner({
      secret: SECRET,
      publicBaseUrl: 'https://alertes.example.test/',
      now: () => NOW,
    }).acknowledgementUrl('token'),
    'https://alertes.example.test/ack/token',
  );
});

test('tampering with payload or signature is rejected', () => {
  const signer = makeSigner();
  const token = signer.issue({
    episodeId: 7,
    nonce: 'nonce-7',
    expiresAt: signer.nextExpiry(),
  });
  const [payload, signature] = token.split('.');

  const changedPayload =
    (payload[0] === 'A' ? 'B' : 'A') + payload.slice(1) + '.' + signature;
  assert.deepEqual(signer.verify(changedPayload), { ok: false, reason: 'invalid_signature' });

  const changedSignature =
    payload + '.' + (signature[0] === 'A' ? 'B' : 'A') + signature.slice(1);
  assert.deepEqual(signer.verify(changedSignature), { ok: false, reason: 'invalid_signature' });

  const otherSecret = new AckTokenSigner({
    secret: 'another-secret-that-is-long-enough',
    publicBaseUrl: BASE_URL,
    now: () => NOW,
  });
  assert.deepEqual(otherSecret.verify(token), { ok: false, reason: 'invalid_signature' });
});

test('absent or malformed tokens are rejected without throwing', () => {
  const signer = makeSigner();
  for (const token of [undefined, null, '', 'abc', 'a.b.c', '...', 'a.%%%', 'x'.repeat(3000)]) {
    const result = signer.verify(token);
    assert.equal(result.ok, false);
    assert.ok(['malformed', 'invalid_signature'].includes(result.reason));
  }
});

test('expired tokens are rejected at and after the expiry instant', () => {
  let current = NOW;
  const signer = makeSigner({ now: () => current, ttlMs: 1000 });
  const expiresAt = signer.nextExpiry();
  const token = signer.issue({ episodeId: 1, nonce: 'n', expiresAt });

  current = new Date(Date.parse(expiresAt) - 1);
  assert.equal(signer.verify(token).ok, true);

  current = new Date(Date.parse(expiresAt));
  assert.deepEqual(signer.verify(token), { ok: false, reason: 'expired' });

  current = new Date(Date.parse(expiresAt) + 60_000);
  assert.deepEqual(signer.verify(token), { ok: false, reason: 'expired' });
});

test('a random secret is generated once and persisted in app_secrets', (t) => {
  const { repositories } = openTestDatabase(t);
  const first = createAckTokenSigner({
    appSecrets: repositories.appSecrets,
    publicBaseUrl: BASE_URL,
    now: () => NOW,
  });
  const stored = repositories.appSecrets.get(ACK_TOKEN_SECRET_NAME);
  assert.equal(typeof stored, 'string');
  assert.ok(stored.length >= 32);

  const second = createAckTokenSigner({
    appSecrets: repositories.appSecrets,
    publicBaseUrl: BASE_URL,
    now: () => NOW,
  });
  assert.equal(second.secret, first.secret);

  const token = first.issue({
    episodeId: 3,
    nonce: 'persisted',
    expiresAt: first.nextExpiry(),
  });
  assert.equal(second.verify(token).ok, true);
});

test('an explicit secret is used without touching app_secrets', () => {
  const signer = createAckTokenSigner({
    secret: SECRET,
    publicBaseUrl: BASE_URL,
    now: () => NOW,
  });
  assert.equal(signer.secret, SECRET);
  assert.throws(
    () => createAckTokenSigner({ publicBaseUrl: BASE_URL }),
    /appSecrets/,
  );
});
