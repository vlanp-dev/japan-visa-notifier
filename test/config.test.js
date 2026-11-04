import assert from 'node:assert/strict';
import test from 'node:test';
import {
  ConfigError,
  DEVELOPMENT_OPERATOR_PASSWORD,
  DEVELOPMENT_OPERATOR_USERNAME,
  loadConfig,
} from '../src/config.js';

test('development has safe, usable defaults', () => {
  const config = loadConfig({}, { cwd: '/tmp/example-project' });
  assert.equal(config.appEnv, 'development');
  assert.equal(config.isProduction, false);
  assert.equal(config.host, '127.0.0.1');
  assert.equal(config.port, 3000);
  assert.equal(config.dataDir, '/tmp/example-project/data');
  assert.equal(config.databasePath, '/tmp/example-project/data/alerts.sqlite');
  assert.equal(config.resendApiKeyPath, '/tmp/example-project/data/resend-api-key');
  assert.equal(config.publicBaseUrl, 'http://127.0.0.1:3000');
  assert.equal(config.operator.username, DEVELOPMENT_OPERATOR_USERNAME);
  assert.equal(config.operator.password, DEVELOPMENT_OPERATOR_PASSWORD);
  assert.equal(config.logLevel, 'info');
  assert.equal(config.maxBodyBytes, 65536);
  assert.equal(config.secureCookies, false);
  assert.equal(config.ackTokenSecret, undefined);
});

test('explicit environment values are honored and normalized', () => {
  const config = loadConfig(
    {
      APP_ENV: 'production',
      HOST: '0.0.0.0',
      PORT: '8443',
      DATA_DIR: '/srv/ambassade',
      DATABASE_PATH: 'state/alerts.sqlite',
      RESEND_API_KEY_FILE: 'secrets/resend-key',
      PUBLIC_BASE_URL: 'https://alerts.vlanp.com/',
      OPERATOR_USERNAME: 'operator-prod',
      OPERATOR_PASSWORD: 'a-long-production-password',
      LOG_LEVEL: 'warn',
      MAX_BODY_BYTES: '4096',
      ACK_TOKEN_SECRET: 'a-production-ack-token-secret-32chars',
    },
    { cwd: '/ignored' },
  );
  assert.equal(config.isProduction, true);
  assert.equal(config.host, '0.0.0.0');
  assert.equal(config.port, 8443);
  assert.equal(config.dataDir, '/srv/ambassade');
  assert.equal(config.databasePath, '/srv/ambassade/state/alerts.sqlite');
  assert.equal(config.resendApiKeyPath, '/srv/ambassade/secrets/resend-key');
  assert.equal(config.publicBaseUrl, 'https://alerts.vlanp.com');
  assert.equal(config.secureCookies, true);
  assert.equal(config.logLevel, 'warn');
  assert.equal(config.maxBodyBytes, 4096);
  assert.equal(config.ackTokenSecret, 'a-production-ack-token-secret-32chars');
});

test('production rejects a short acknowledgement signing secret', () => {
  assert.throws(
    () =>
      loadConfig({
        APP_ENV: 'production',
        PUBLIC_BASE_URL: 'https://alerts.example.test',
        OPERATOR_USERNAME: 'operator-prod',
        OPERATOR_PASSWORD: 'a-long-production-password',
        ACK_TOKEN_SECRET: 'too-short',
      }),
    (error) => error instanceof ConfigError && /ACK_TOKEN_SECRET/.test(error.message),
  );
});

test('port zero is accepted for ephemeral test servers', () => {
  assert.equal(loadConfig({ PORT: '0' }).port, 0);
});

test('production fails closed without explicit operator credentials', () => {
  const base = {
    APP_ENV: 'production',
    PUBLIC_BASE_URL: 'https://alerts.example.test',
  };
  assert.throws(
    () => loadConfig({ ...base, OPERATOR_PASSWORD: 'a-long-production-password' }),
    (error) => error instanceof ConfigError && /OPERATOR_USERNAME/.test(error.message),
  );
  assert.throws(
    () => loadConfig({ ...base, OPERATOR_USERNAME: 'operator-prod' }),
    (error) => error instanceof ConfigError && /OPERATOR_PASSWORD/.test(error.message),
  );
});

test('production rejects development credentials and short passwords', () => {
  const base = {
    APP_ENV: 'production',
    PUBLIC_BASE_URL: 'https://alerts.example.test',
  };
  assert.throws(
    () =>
      loadConfig({
        ...base,
        OPERATOR_USERNAME: DEVELOPMENT_OPERATOR_USERNAME,
        OPERATOR_PASSWORD: 'a-long-production-password',
      }),
    ConfigError,
  );
  assert.throws(
    () =>
      loadConfig({
        ...base,
        OPERATOR_USERNAME: 'operator-prod',
        OPERATOR_PASSWORD: DEVELOPMENT_OPERATOR_PASSWORD,
      }),
    ConfigError,
  );
  assert.throws(
    () =>
      loadConfig({
        ...base,
        OPERATOR_USERNAME: 'operator-prod',
        OPERATOR_PASSWORD: 'short',
      }),
    ConfigError,
  );
});

test('production requires a public base URL', () => {
  assert.throws(
    () =>
      loadConfig({
        APP_ENV: 'production',
        OPERATOR_USERNAME: 'operator-prod',
        OPERATOR_PASSWORD: 'a-long-production-password',
      }),
    (error) => error instanceof ConfigError && /PUBLIC_BASE_URL/.test(error.message),
  );
});

test('invalid scalar configuration is rejected with a clear config error', () => {
  assert.throws(() => loadConfig({ APP_ENV: 'staging' }), ConfigError);
  assert.throws(() => loadConfig({ PORT: 'abc' }), ConfigError);
  assert.throws(() => loadConfig({ PORT: '70000' }), ConfigError);
  assert.throws(() => loadConfig({ LOG_LEVEL: 'verbose' }), ConfigError);
  assert.throws(() => loadConfig({ MAX_BODY_BYTES: '0' }), ConfigError);
  assert.throws(() => loadConfig({ HOST: 'http://localhost' }), ConfigError);
  assert.throws(() => loadConfig({ PUBLIC_BASE_URL: 'ftp://example.test' }), ConfigError);
  assert.throws(() => loadConfig({ PUBLIC_BASE_URL: 'https://user:pass@example.test' }), ConfigError);
  assert.throws(() => loadConfig({ PUBLIC_BASE_URL: 'https://example.test/path?x=1' }), ConfigError);
});
