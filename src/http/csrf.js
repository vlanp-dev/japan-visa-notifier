import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';
import { appendSetCookie, serializeCookie } from './cookies.js';

export const CSRF_FIELD_NAME = '_csrf';
const TOKEN_PATTERN = /^[a-f0-9]{64}$/;
const TOKEN_MAX_AGE_SECONDS = 8 * 60 * 60;

export function generateCsrfToken() {
  return randomBytes(32).toString('hex');
}

export function isValidCsrfToken(token) {
  return typeof token === 'string' && TOKEN_PATTERN.test(token);
}

export function csrfTokensMatch(left, right) {
  if (!isValidCsrfToken(left) || !isValidCsrfToken(right)) {
    return false;
  }
  const leftDigest = createHash('sha256').update(left, 'utf8').digest();
  const rightDigest = createHash('sha256').update(right, 'utf8').digest();
  return timingSafeEqual(leftDigest, rightDigest);
}

/**
 * Ensure the double-submit cookie exists. The cookie is HttpOnly because the
 * server renders the token into forms; JavaScript never needs to read it.
 */
export function ensureCsrfCookie(req, res, config, cookies) {
  const existing = cookies[config.csrfCookieName];
  if (isValidCsrfToken(existing)) {
    return existing;
  }
  const token = generateCsrfToken();
  appendSetCookie(
    res,
    serializeCookie(config.csrfCookieName, token, {
      path: '/',
      maxAge: TOKEN_MAX_AGE_SECONDS,
      httpOnly: true,
      secure: config.secureCookies,
      sameSite: 'Lax',
    }),
  );
  return token;
}

/** Extract the submitted CSRF token from the header or a form field. */
export function submittedCsrfToken(req, form, headerName) {
  const header = req.headers[headerName];
  if (typeof header === 'string' && header.trim() !== '') {
    return header.trim();
  }
  if (form && typeof form[CSRF_FIELD_NAME] === 'string') {
    return form[CSRF_FIELD_NAME];
  }
  return null;
}
