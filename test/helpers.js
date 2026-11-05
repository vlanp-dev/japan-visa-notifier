import { mkdtempSync, rmSync } from 'node:fs';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadConfig } from '../src/config.js';
import { openDatabase } from '../src/db.js';
import { createApp } from '../src/http/app.js';
import { createLogger } from '../src/logger.js';
import { createRepositories } from '../src/repositories/index.js';

export const TEST_OPERATOR_USERNAME = 'test-operator';
export const TEST_OPERATOR_PASSWORD = 'test-operator-password';

export function makeTempDir(t) {
  const dir = mkdtempSync(join(tmpdir(), 'ambassade-alerts-test-'));
  t.after(() => {
    rmSync(dir, { recursive: true, force: true });
  });
  return dir;
}

export function baseTestEnv(dataDir, overrides = {}) {
  return {
    APP_ENV: 'test',
    HOST: '127.0.0.1',
    PORT: '0',
    DATA_DIR: dataDir,
    PUBLIC_BASE_URL: 'http://127.0.0.1:3000',
    OPERATOR_USERNAME: TEST_OPERATOR_USERNAME,
    OPERATOR_PASSWORD: TEST_OPERATOR_PASSWORD,
    LOG_LEVEL: 'debug',
    ...overrides,
  };
}

export function openTestDatabase(t, { env = {}, now } = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'ambassade-alerts-db-'));
  const config = loadConfig(baseTestEnv(dir, env));
  const db = openDatabase(config.databasePath);
  const repositories = createRepositories(db, now === undefined ? {} : { now });
  t.after(() => {
    try {
      db.close();
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
  return { db, config, repositories, dir };
}

function listen(server) {
  return new Promise((resolve, reject) => {
    const onError = (error) => {
      server.off('listening', onListening);
      reject(error);
    };
    const onListening = () => {
      server.off('error', onError);
      resolve(server.address());
    };
    server.once('error', onError);
    server.once('listening', onListening);
    server.listen(0, '127.0.0.1');
  });
}

function closeServer(server) {
  return new Promise((resolve) => {
    if (!server.listening) {
      resolve();
      return;
    }
    server.close(() => resolve());
    server.closeIdleConnections?.();
  });
}

export async function createTestHarness(t, { env = {}, registerRoutes } = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'ambassade-alerts-http-'));
  const config = loadConfig(baseTestEnv(dir, env));
  const lines = [];
  const stream = {
    write(chunk) {
      lines.push(String(chunk));
      return true;
    },
  };
  const logger = createLogger({
    level: 'debug',
    stream,
    errorStream: stream,
    secrets: [config.operator.password],
  });
  const db = openDatabase(config.databasePath);
  const repositories = createRepositories(db);
  const app = createApp({
    config,
    logger,
    db,
    repositories,
    registerRoutes: registerRoutes
      ? (application) => registerRoutes(application, { config, logger, db, repositories })
      : undefined,
  });
  const server = createServer((req, res) => {
    void app.handle(req, res);
  });
  const address = await listen(server);
  const baseUrl = `http://127.0.0.1:${address.port}`;

  t.after(async () => {
    await closeServer(server);
    db.close();
    rmSync(dir, { recursive: true, force: true });
  });

  return {
    dir,
    config,
    logger,
    lines,
    db,
    repositories,
    app,
    server,
    baseUrl,
    operatorAuthorization: basicAuthorization(TEST_OPERATOR_USERNAME, TEST_OPERATOR_PASSWORD),
  };
}

export function basicAuthorization(username, password) {
  return `Basic ${Buffer.from(`${username}:${password}`, 'utf8').toString('base64')}`;
}

export function csrfFromResponse(response) {
  const setCookies = response.headers.getSetCookie();
  const cookie = setCookies.find((value) => value.startsWith('csrf_token='));
  if (!cookie) {
    return null;
  }
  const [pair] = cookie.split(';');
  const token = pair.slice('csrf_token='.length);
  return { cookie: pair, token };
}
