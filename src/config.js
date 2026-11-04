import path from 'node:path';

export const APP_ENVIRONMENTS = Object.freeze(['development', 'test', 'production']);
export const LOG_LEVELS = Object.freeze(['debug', 'info', 'warn', 'error', 'silent']);

export const DEVELOPMENT_OPERATOR_USERNAME = 'operator';
export const DEVELOPMENT_OPERATOR_PASSWORD = 'development-password';

const DEFAULT_PORT = 3000;
const DEFAULT_HOST = '127.0.0.1';
const DEFAULT_MAX_BODY_BYTES = 64 * 1024;
const DEFAULT_LOG_LEVEL = 'info';
const MIN_BODY_BYTES = 1;
const MAX_BODY_BYTES = 10 * 1024 * 1024;
const MIN_PRODUCTION_PASSWORD_LENGTH = 12;

export class ConfigError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ConfigError';
    this.code = 'CONFIG_INVALID';
  }
}

function readOptionalString(env, name) {
  const raw = env[name];
  if (raw === undefined || raw === null) {
    return undefined;
  }
  const value = String(raw).trim();
  return value === '' ? undefined : value;
}

function parseInteger(env, name, fallback, { min, max }) {
  const raw = readOptionalString(env, name);
  if (raw === undefined) {
    return fallback;
  }
  if (!/^\d+$/.test(raw)) {
    throw new ConfigError(`${name} doit être un entier positif.`);
  }
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < min || value > max) {
    throw new ConfigError(`${name} doit être compris entre ${min} et ${max}.`);
  }
  return value;
}

function parsePublicBaseUrl(raw, fallback) {
  let parsed;
  try {
    parsed = new URL(raw ?? fallback);
  } catch {
    throw new ConfigError('PUBLIC_BASE_URL doit être une URL absolue valide.');
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new ConfigError('PUBLIC_BASE_URL doit utiliser http ou https.');
  }
  if (parsed.username !== '' || parsed.password !== '') {
    throw new ConfigError('PUBLIC_BASE_URL ne doit pas contenir d’identifiants.');
  }
  if (parsed.search !== '' || parsed.hash !== '') {
    throw new ConfigError('PUBLIC_BASE_URL ne doit pas contenir de requête ni de fragment.');
  }
  const pathname = parsed.pathname.replace(/\/+$/, '');
  return `${parsed.origin}${pathname}`;
}

export function loadConfig(env = process.env, { cwd = process.cwd() } = {}) {
  const appEnv = readOptionalString(env, 'APP_ENV') ?? 'development';
  if (!APP_ENVIRONMENTS.includes(appEnv)) {
    throw new ConfigError(`APP_ENV doit valoir l’une des valeurs suivantes : ${APP_ENVIRONMENTS.join(', ')}.`);
  }
  const isProduction = appEnv === 'production';

  const host = readOptionalString(env, 'HOST') ?? DEFAULT_HOST;
  if (host.includes('/') || host.includes('://') || /\s/.test(host)) {
    throw new ConfigError('HOST doit être un nom d’hôte ou une adresse IP valide.');
  }

  const port = parseInteger(env, 'PORT', DEFAULT_PORT, { min: 0, max: 65535 });

  const defaultPublicBaseUrl = `http://127.0.0.1:${port}`;
  const rawPublicBaseUrl = readOptionalString(env, 'PUBLIC_BASE_URL');
  if (isProduction && rawPublicBaseUrl === undefined) {
    throw new ConfigError('PUBLIC_BASE_URL est obligatoire en production.');
  }
  const publicBaseUrl = parsePublicBaseUrl(rawPublicBaseUrl, defaultPublicBaseUrl);

  const dataDir = path.resolve(cwd, readOptionalString(env, 'DATA_DIR') ?? './data');
  const rawDatabasePath = readOptionalString(env, 'DATABASE_PATH') ?? 'alerts.sqlite';
  const databasePath =
    rawDatabasePath === ':memory:' ? ':memory:' : path.resolve(dataDir, rawDatabasePath);

  const devFallbackUsername = isProduction ? undefined : DEVELOPMENT_OPERATOR_USERNAME;
  const devFallbackPassword = isProduction ? undefined : DEVELOPMENT_OPERATOR_PASSWORD;
  const username = readOptionalString(env, 'OPERATOR_USERNAME') ?? devFallbackUsername;
  const password = readOptionalString(env, 'OPERATOR_PASSWORD') ?? devFallbackPassword;

  if (isProduction) {
    if (username === undefined) {
      throw new ConfigError('OPERATOR_USERNAME est obligatoire en production.');
    }
    if (password === undefined) {
      throw new ConfigError('OPERATOR_PASSWORD est obligatoire en production.');
    }
    if (username === DEVELOPMENT_OPERATOR_USERNAME || password === DEVELOPMENT_OPERATOR_PASSWORD) {
      throw new ConfigError('Les identifiants de développement sont interdits en production.');
    }
    if (password.length < MIN_PRODUCTION_PASSWORD_LENGTH) {
      throw new ConfigError(
        `OPERATOR_PASSWORD doit contenir au moins ${MIN_PRODUCTION_PASSWORD_LENGTH} caractères en production.`,
      );
    }
  }

  const logLevel = readOptionalString(env, 'LOG_LEVEL') ?? DEFAULT_LOG_LEVEL;
  if (!LOG_LEVELS.includes(logLevel)) {
    throw new ConfigError(`LOG_LEVEL doit valoir l’une des valeurs suivantes : ${LOG_LEVELS.join(', ')}.`);
  }

  const maxBodyBytes = parseInteger(env, 'MAX_BODY_BYTES', DEFAULT_MAX_BODY_BYTES, {
    min: MIN_BODY_BYTES,
    max: MAX_BODY_BYTES,
  });

  return Object.freeze({
    appEnv,
    isProduction,
    host,
    port,
    dataDir,
    databasePath,
    publicBaseUrl,
    operator: Object.freeze({ username, password }),
    logLevel,
    maxBodyBytes,
    secureCookies: publicBaseUrl.startsWith('https:'),
    csrfCookieName: 'csrf_token',
    csrfHeaderName: 'x-csrf-token',
  });
}
