import { createHash, timingSafeEqual } from 'node:crypto';

const BASIC_PREFIX = /^Basic\s+(.+)$/i;
const BASE64 = /^[A-Za-z0-9+/]+={0,2}$/;

function digest(value) {
  return createHash('sha256').update(value, 'utf8').digest();
}

function safeEqual(left, right) {
  return timingSafeEqual(digest(left), digest(right));
}

/** Parse an Authorization: Basic header without throwing. */
export function parseBasicAuthorization(header) {
  if (typeof header !== 'string') {
    return null;
  }
  const match = BASIC_PREFIX.exec(header.trim());
  if (!match) {
    return null;
  }
  const encoded = match[1].trim();
  if (!BASE64.test(encoded)) {
    return null;
  }
  let decoded;
  try {
    decoded = Buffer.from(encoded, 'base64').toString('utf8');
  } catch {
    return null;
  }
  const separator = decoded.indexOf(':');
  if (separator < 0) {
    return null;
  }
  return {
    username: decoded.slice(0, separator),
    password: decoded.slice(separator + 1),
  };
}

/**
 * Verify a Basic authorization header against the configured operator
 * credentials. Both comparisons always run to avoid leaking which field failed.
 */
export function checkBasicAuth(header, credentials) {
  const parsed = parseBasicAuthorization(header);
  if (parsed === null) {
    return false;
  }
  const usernameMatches = safeEqual(parsed.username, credentials.username);
  const passwordMatches = safeEqual(parsed.password, credentials.password);
  return usernameMatches && passwordMatches;
}
