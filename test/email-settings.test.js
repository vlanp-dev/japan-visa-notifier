import assert from 'node:assert/strict';
import {
  chmodSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  statSync,
  writeFileSync,
} from 'node:fs';
import { dirname, join } from 'node:path';
import test from 'node:test';
import {
  EMAIL_SETTING_KEYS,
  EmailConfigurationError,
  EmailSettingsService,
} from '../src/email/settings.js';
import { openTestDatabase } from './helpers.js';

const NOW = new Date('2026-11-04T10:00:00.000Z');
const API_KEY = 're_test_key_123456';

function setup(t) {
  const context = openTestDatabase(t, { now: () => NOW });
  const apiKeyPath = join(context.dir, 'private', 'resend-api-key');
  const service = new EmailSettingsService({
    repositories: context.repositories,
    apiKeyPath,
  });
  return { ...context, service, apiKeyPath };
}

function fileMode(filePath) {
  return statSync(filePath).mode & 0o777;
}

test('a fresh configuration reports every missing field without a provider call', (t) => {
  const { service } = setup(t);
  const state = service.getConfigurationState();

  assert.equal(state.complete, false);
  assert.deepEqual(state.missingFields, ['sender', 'recipient', 'sendingSubdomain', 'apiKey']);
  assert.deepEqual(state.missing, [
    'expéditeur',
    'destinataire',
    'sous-domaine d’envoi',
    'clé API Resend',
  ]);
  assert.equal(state.sender, null);
  assert.equal(state.recipient, null);
  assert.equal(state.sendingSubdomain, null);
  assert.equal(state.apiKeyConfigured, false);
  assert.equal(service.isConfigured(), false);
  assert.equal(service.readApiKey(), null);
});

test('sender, recipient and sending subdomain persist while the key stays out of SQLite', (t) => {
  const { service, repositories, apiKeyPath } = setup(t);
  const state = service.update({
    sender: 'alertes@alertes.vlanp.com',
    recipient: 'operateur@example.test',
    sendingSubdomain: 'Alertes.VLANP.com',
    apiKey: API_KEY,
  });

  assert.equal(state.complete, true);
  assert.equal(state.sendingSubdomain, 'alertes.vlanp.com');
  assert.equal(repositories.settings.get(EMAIL_SETTING_KEYS.sender), 'alertes@alertes.vlanp.com');
  assert.equal(repositories.settings.get(EMAIL_SETTING_KEYS.recipient), 'operateur@example.test');
  assert.equal(repositories.settings.get(EMAIL_SETTING_KEYS.sendingSubdomain), 'alertes.vlanp.com');

  assert.equal(fileMode(apiKeyPath), 0o600);
  assert.equal(readFileSync(apiKeyPath, 'utf8'), `${API_KEY}\n`);
  assert.equal(service.readApiKey(), API_KEY);

  const stored = repositories.settings.getAll();
  assert.equal(Object.values(stored).includes(API_KEY), false);
  assert.equal(JSON.stringify(stored).includes(API_KEY), false);
  assert.equal(JSON.stringify(state).includes(API_KEY), false);
});

test('blank fields keep their stored value, including the key', (t) => {
  const { service, apiKeyPath } = setup(t);
  const full = {
    sender: 'alertes@alertes.vlanp.com',
    recipient: 'operateur@example.test',
    sendingSubdomain: 'alertes',
  };
  service.update({ ...full, apiKey: API_KEY });
  const keyBefore = readFileSync(apiKeyPath, 'utf8');

  const unchanged = service.update({
    sender: '',
    recipient: '   ',
    sendingSubdomain: '\t',
    apiKey: '',
  });
  assert.deepEqual(
    {
      sender: unchanged.sender,
      recipient: unchanged.recipient,
      sendingSubdomain: unchanged.sendingSubdomain,
    },
    full,
  );
  assert.equal(unchanged.apiKeyConfigured, true);
  assert.equal(readFileSync(apiKeyPath, 'utf8'), keyBefore);

  service.update({ recipient: 'nouveau@example.test' });
  const afterPartial = service.getSettings();
  assert.equal(afterPartial.sender, full.sender);
  assert.equal(afterPartial.recipient, 'nouveau@example.test');
  assert.equal(afterPartial.sendingSubdomain, full.sendingSubdomain);
  assert.equal(service.readApiKey(), API_KEY);

  service.update({});
  assert.deepEqual(service.getSettings(), afterPartial);
});

test('rewriting the key atomically tightens an existing permissive file to 0600', (t) => {
  const { service, apiKeyPath } = setup(t);
  mkdirSync(dirname(apiKeyPath), { recursive: true });
  writeFileSync(apiKeyPath, 'old-value\n', { mode: 0o644 });
  chmodSync(apiKeyPath, 0o644);
  assert.equal(fileMode(apiKeyPath), 0o644);

  service.update({ apiKey: 're_replacement_key' });

  assert.equal(fileMode(apiKeyPath), 0o600);
  assert.equal(readFileSync(apiKeyPath, 'utf8'), 're_replacement_key\n');
  assert.deepEqual(
    readdirSync(dirname(apiKeyPath)).filter((name) => name.endsWith('.tmp')),
    [],
  );
});

test('the key is re-read from disk on every call', (t) => {
  const { service, apiKeyPath } = setup(t);
  service.update({ apiKey: 're_first_key' });
  assert.equal(service.readApiKey(), 're_first_key');

  writeFileSync(apiKeyPath, 're_second_key\n', { mode: 0o600 });
  assert.equal(service.readApiKey(), 're_second_key');

  writeFileSync(apiKeyPath, '', { mode: 0o600 });
  assert.equal(service.readApiKey(), null);
  assert.equal(service.getConfigurationState().apiKeyConfigured, false);
});

test('invalid values are rejected before anything is persisted and never echo the key', (t) => {
  const { service, apiKeyPath } = setup(t);
  const cases = [
    { sender: 'not-an-email' },
    { sender: 'a@b.test, c@d.test' },
    { recipient: 'a@b.test;c@d.test' },
    { sendingSubdomain: 'https://alertes.vlanp.com' },
    { sendingSubdomain: 'alertes vlanp' },
    { sender: 42 },
    { apiKey: 42 },
    { apiKey: 'secret\nvalue' },
  ];

  for (const input of cases) {
    assert.throws(() => service.update(input), TypeError);
  }
  const keyError = (() => {
    try {
      service.update({ apiKey: 'secret\nvalue' });
    } catch (error) {
      return error;
    }
    return null;
  })();
  assert.ok(keyError);
  assert.doesNotMatch(keyError.message, /secret/);
  assert.equal(service.getConfigurationState().complete, false);
  assert.equal(existsSync(apiKeyPath), false);
});

test('an unreadable key file is reported as missing without taking the service down', (t) => {
  const { service, apiKeyPath } = setup(t);
  mkdirSync(apiKeyPath, { recursive: true });

  const state = service.getConfigurationState();
  assert.equal(state.complete, false);
  assert.equal(state.apiKeyConfigured, false);
  assert.ok(state.apiKeyError);
  assert.match(state.apiKeyError, /illisible/);
  assert.doesNotMatch(state.apiKeyError, /re_/);
  assert.deepEqual(state.missingFields, ['sender', 'recipient', 'sendingSubdomain', 'apiKey']);
});

test('a failed key write raises a safe configuration error', (t) => {
  const { service, apiKeyPath } = setup(t);
  mkdirSync(apiKeyPath, { recursive: true });

  assert.throws(
    () => service.update({ apiKey: 're_super_secret_value' }),
    (error) =>
      error instanceof EmailConfigurationError &&
      /échoué/.test(error.message) &&
      !error.message.includes('re_super_secret_value'),
  );
});
