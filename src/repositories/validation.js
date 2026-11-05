export function requireNonEmptyString(value, name) {
  if (typeof value !== 'string' || value.trim() === '') {
    throw new TypeError(`${name} must be a non-empty string.`);
  }
  return value;
}

export function requireString(value, name) {
  if (typeof value !== 'string') {
    throw new TypeError(`${name} must be a string.`);
  }
  return value;
}

export function requireOptionalString(value, name) {
  if (value === undefined || value === null) {
    return null;
  }
  if (typeof value !== 'string') {
    throw new TypeError(`${name} must be a string or null.`);
  }
  return value;
}

export function isoNow(now) {
  const value = now();
  if (!(value instanceof Date) || Number.isNaN(value.getTime())) {
    throw new TypeError('Clock must return a valid Date.');
  }
  return value.toISOString();
}

export function requireIsoTimestamp(value, name) {
  if (typeof value !== 'string' || Number.isNaN(Date.parse(value))) {
    throw new TypeError(`${name} must be an ISO-8601 timestamp string.`);
  }
  return value;
}

export function toPositiveInteger(value, name) {
  if (!Number.isInteger(value) || value < 1) {
    throw new TypeError(`${name} must be a positive integer.`);
  }
  return value;
}
