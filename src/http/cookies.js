const COOKIE_NAME = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/;
const COOKIE_VALUE = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]*$/;

export function parseCookies(header) {
  const cookies = Object.create(null);
  if (typeof header !== 'string' || header === '') {
    return cookies;
  }
  for (const part of header.split(';')) {
    const separator = part.indexOf('=');
    if (separator < 0) {
      continue;
    }
    const name = part.slice(0, separator).trim();
    let value = part.slice(separator + 1).trim();
    if (name === '') {
      continue;
    }
    if (value.startsWith('"') && value.endsWith('"')) {
      value = value.slice(1, -1);
    }
    try {
      cookies[name] = decodeURIComponent(value);
    } catch {
      cookies[name] = value;
    }
  }
  return cookies;
}

export function serializeCookie(name, value, options = {}) {
  if (!COOKIE_NAME.test(name)) {
    throw new TypeError('Invalid cookie name.');
  }
  if (!COOKIE_VALUE.test(value)) {
    throw new TypeError('Invalid cookie value.');
  }
  const parts = [`${name}=${value}`];
  parts.push(`Path=${options.path ?? '/'}`);
  if (options.maxAge !== undefined) {
    if (!Number.isInteger(options.maxAge) || options.maxAge < 0) {
      throw new TypeError('maxAge must be a non-negative integer.');
    }
    parts.push(`Max-Age=${options.maxAge}`);
  }
  if (options.httpOnly !== false) {
    parts.push('HttpOnly');
  }
  if (options.secure === true) {
    parts.push('Secure');
  }
  if (options.sameSite !== undefined) {
    if (!['Strict', 'Lax', 'None'].includes(options.sameSite)) {
      throw new TypeError('sameSite must be Strict, Lax or None.');
    }
    parts.push(`SameSite=${options.sameSite}`);
  }
  return parts.join('; ');
}

export function appendSetCookie(res, cookie) {
  const existing = res.getHeader('Set-Cookie');
  if (existing === undefined) {
    res.setHeader('Set-Cookie', cookie);
    return;
  }
  res.setHeader('Set-Cookie', Array.isArray(existing) ? [...existing, cookie] : [existing, cookie]);
}
