import { inspect } from 'node:util';

export const LOG_LEVELS = Object.freeze({
  debug: 10,
  info: 20,
  warn: 30,
  error: 40,
  silent: 100,
});

export const REDACTED = '[redacted]';

const SENSITIVE_KEY = /(pass(word|wd)?|secret|token|api[-_]?key|authorization|cookie|credential|private[-_]?key)/i;
const MAX_DEPTH = 6;

function redactString(value, secrets) {
  let result = value;
  for (const secret of secrets) {
    if (typeof secret === 'string' && secret.length >= 4) {
      result = result.split(secret).join(REDACTED);
    }
  }
  return result;
}

function redactValue(value, { key, secrets, seen, depth }) {
  if (key !== undefined && SENSITIVE_KEY.test(key)) {
    return value === null || value === undefined || value === '' ? value : REDACTED;
  }
  if (value === null || value === undefined) {
    return value;
  }
  if (typeof value === 'string') {
    return redactString(value, secrets);
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return value;
  }
  if (typeof value === 'bigint') {
    return value.toString();
  }
  if (typeof value !== 'object') {
    return String(value);
  }
  if (value instanceof Error) {
    return {
      name: value.name,
      message: redactString(value.message, secrets),
      ...(typeof value.code === 'string' ? { code: value.code } : {}),
    };
  }
  if (seen.has(value)) {
    return '[circular]';
  }
  if (depth >= MAX_DEPTH) {
    return '[depth-limit]';
  }
  seen.add(value);
  if (Array.isArray(value)) {
    return value.map((item) => redactValue(item, { secrets, seen, depth: depth + 1 }));
  }
  if (value instanceof Date) {
    return value.toISOString();
  }
  const result = {};
  for (const [entryKey, entryValue] of Object.entries(value)) {
    result[entryKey] = redactValue(entryValue, {
      key: entryKey,
      secrets,
      seen,
      depth: depth + 1,
    });
  }
  return result;
}

/**
 * Return a structurally redacted copy of a value. Sensitive-looking keys are
 * replaced entirely, and known secret strings are replaced wherever they occur.
 */
export function redact(value, { secrets = [] } = {}) {
  return redactValue(value, {
    secrets: secrets.filter((secret) => typeof secret === 'string' && secret.length > 0),
    seen: new WeakSet(),
    depth: 0,
  });
}

function writeLine(stream, line) {
  if (!stream || typeof stream.write !== 'function') {
    return;
  }
  try {
    stream.write(`${line}\n`);
  } catch {
    // Logging must never take the service down.
  }
}

/**
 * Create a JSON-lines logger. Log records are written one object per line and
 * every field (including the message) passes through the redactor.
 */
export function createLogger({
  level = 'info',
  stream = process.stdout,
  errorStream = stream,
  secrets = [],
  bindings = {},
  now = () => new Date(),
} = {}) {
  if (!(level in LOG_LEVELS)) {
    throw new Error(`Unknown log level: ${level}`);
  }
  const knownSecrets = secrets.filter((secret) => typeof secret === 'string' && secret.length > 0);

  function emit(entryLevel, message, fields) {
    if (LOG_LEVELS[entryLevel] < LOG_LEVELS[level]) {
      return;
    }
    const record = {
      time: now().toISOString(),
      level: entryLevel,
      msg: redactString(String(message), knownSecrets),
      ...redact(
        { ...bindings, ...(fields ?? {}) },
        { secrets: knownSecrets },
      ),
    };
    const target = entryLevel === 'error' || entryLevel === 'warn' ? errorStream : stream;
    let line;
    try {
      line = JSON.stringify(record);
    } catch {
      line = JSON.stringify({
        time: record.time,
        level: entryLevel,
        msg: 'log_serialization_failed',
      });
    }
    writeLine(target, line);
  }

  return {
    level,
    debug: (message, fields) => emit('debug', message, fields),
    info: (message, fields) => emit('info', message, fields),
    warn: (message, fields) => emit('warn', message, fields),
    error: (message, fields) => emit('error', message, fields),
    child(extraBindings = {}) {
      return createLogger({
        level,
        stream,
        errorStream,
        secrets: knownSecrets,
        bindings: { ...bindings, ...extraBindings },
        now,
      });
    },
  };
}

/** Render a value for interactive debugging with secrets removed. */
export function inspectRedacted(value, { secrets = [] } = {}) {
  return inspect(redact(value, { secrets }), { depth: 6, breakLength: 120 });
}
