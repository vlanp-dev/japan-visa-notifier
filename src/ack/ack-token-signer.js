import { createHmac, randomBytes, timingSafeEqual } from 'node:crypto';

export const ACK_TOKEN_SECRET_NAME = 'ack_token_secret';
export const DEFAULT_ACK_TOKEN_TTL_MS = 7 * 24 * 60 * 60 * 1000;

const TOKEN_VERSION = 1;
const MAX_TOKEN_LENGTH = 2048;
const BASE64URL = /^[A-Za-z0-9_-]+$/;

function requireSecret(secret) {
  if (typeof secret !== 'string' || secret === '') {
    throw new TypeError('secret must be a non-empty string.');
  }
  return secret;
}

function requirePublicBaseUrl(value) {
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new TypeError('publicBaseUrl must be an absolute http(s) URL.');
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new TypeError('publicBaseUrl must use http or https.');
  }
  if (parsed.username !== '' || parsed.password !== '' || parsed.search !== '' || parsed.hash !== '') {
    throw new TypeError('publicBaseUrl must be an origin without credentials, query or fragment.');
  }
  const pathname = parsed.pathname.replace(/\/+$/, '');
  if (pathname !== '') {
    throw new TypeError('publicBaseUrl must not contain a path; acknowledgement links are served at the root.');
  }
  return parsed.origin;
}

function requirePositiveInteger(value, name) {
  if (!Number.isInteger(value) || value < 1) {
    throw new TypeError(`${name} must be a positive integer.`);
  }
  return value;
}

function requireNonce(value) {
  if (typeof value !== 'string' || value === '' || value.length > 200) {
    throw new TypeError('nonce must be a non-empty string of at most 200 characters.');
  }
  return value;
}

function requireExpiry(value) {
  if (typeof value !== 'string' || Number.isNaN(Date.parse(value))) {
    throw new TypeError('expiresAt must be an ISO-8601 timestamp string.');
  }
  return Date.parse(value);
}

function safeEqual(left, right) {
  const leftBuffer = Buffer.from(left, 'utf8');
  const rightBuffer = Buffer.from(right, 'utf8');
  if (leftBuffer.length !== rightBuffer.length) {
    return false;
  }
  return timingSafeEqual(leftBuffer, rightBuffer);
}

/**
 * Issue and verify URL-safe acknowledgement tokens. A token is a base64url
 * JSON payload followed by a base64url HMAC-SHA256 signature over that exact
 * payload. The payload carries the episode id, a random nonce and an absolute
 * expiry in epoch milliseconds.
 */
export class AckTokenSigner {
  constructor({ secret, publicBaseUrl, ttlMs = DEFAULT_ACK_TOKEN_TTL_MS, now = () => new Date() } = {}) {
    if (!Number.isInteger(ttlMs) || ttlMs < 1) {
      throw new TypeError('ttlMs must be a positive integer.');
    }
    if (typeof now !== 'function') {
      throw new TypeError('now must be a function.');
    }
    this.secret = requireSecret(secret);
    this.publicBaseUrl = requirePublicBaseUrl(publicBaseUrl);
    this.ttlMs = ttlMs;
    this.now = now;
  }

  /** ISO timestamp at which a token issued now stops being valid. */
  nextExpiry() {
    const current = this.now();
    if (!(current instanceof Date) || Number.isNaN(current.getTime())) {
      throw new TypeError('Clock must return a valid Date.');
    }
    return new Date(current.getTime() + this.ttlMs).toISOString();
  }

  issue({ episodeId, nonce, expiresAt } = {}) {
    requirePositiveInteger(episodeId, 'episodeId');
    requireNonce(nonce);
    const expiresAtMs = requireExpiry(expiresAt);
    const payload = Buffer.from(
      JSON.stringify({ v: TOKEN_VERSION, eid: episodeId, n: nonce, exp: expiresAtMs }),
      'utf8',
    ).toString('base64url');
    return `${payload}.${this.sign(payload)}`;
  }

  /**
   * Verify signature and expiry. Returns a discriminated result; it never
   * throws for user-supplied token input and never mutates state.
   */
  verify(token) {
    if (typeof token !== 'string' || token === '' || token.length > MAX_TOKEN_LENGTH) {
      return { ok: false, reason: 'malformed' };
    }
    const parts = token.split('.');
    if (parts.length !== 2 || !BASE64URL.test(parts[0]) || !BASE64URL.test(parts[1])) {
      return { ok: false, reason: 'malformed' };
    }
    const [encodedPayload, signature] = parts;
    if (!safeEqual(signature, this.sign(encodedPayload))) {
      return { ok: false, reason: 'invalid_signature' };
    }

    let payload;
    try {
      payload = JSON.parse(Buffer.from(encodedPayload, 'base64url').toString('utf8'));
    } catch {
      return { ok: false, reason: 'malformed' };
    }
    if (
      payload === null ||
      typeof payload !== 'object' ||
      payload.v !== TOKEN_VERSION ||
      !Number.isSafeInteger(payload.eid) ||
      payload.eid < 1 ||
      typeof payload.n !== 'string' ||
      payload.n === '' ||
      payload.n.length > 200 ||
      !Number.isSafeInteger(payload.exp) ||
      payload.exp < 1
    ) {
      return { ok: false, reason: 'malformed' };
    }

    const current = this.now();
    if (!(current instanceof Date) || Number.isNaN(current.getTime())) {
      throw new TypeError('Clock must return a valid Date.');
    }
    if (payload.exp <= current.getTime()) {
      return { ok: false, reason: 'expired' };
    }

    return {
      ok: true,
      episodeId: payload.eid,
      nonce: payload.n,
      expiresAt: new Date(payload.exp).toISOString(),
    };
  }

  /** Absolute acknowledgement URL for a token, based on PUBLIC_BASE_URL. */
  acknowledgementUrl(token) {
    if (typeof token !== 'string' || token === '') {
      throw new TypeError('token must be a non-empty string.');
    }
    return `${this.publicBaseUrl}/ack/${encodeURIComponent(token)}`;
  }

  sign(encodedPayload) {
    return createHmac('sha256', this.secret).update(encodedPayload, 'utf8').digest('base64url');
  }
}

/**
 * Build a signer from an explicit ACK_TOKEN_SECRET, or create a strong random
 * secret once and persist it in app_secrets. The stored secret survives
 * restarts, so previously emailed links keep working.
 */
export function createAckTokenSigner({ appSecrets, secret, publicBaseUrl, ttlMs, now } = {}) {
  let resolvedSecret = secret;
  if (resolvedSecret === undefined || resolvedSecret === null || resolvedSecret === '') {
    if (!appSecrets || typeof appSecrets.getOrCreate !== 'function') {
      throw new TypeError('appSecrets repository is required when no secret is configured.');
    }
    resolvedSecret = appSecrets.getOrCreate(ACK_TOKEN_SECRET_NAME, () =>
      randomBytes(32).toString('base64url'),
    );
  }
  return new AckTokenSigner({ secret: resolvedSecret, publicBaseUrl, ttlMs, now });
}
