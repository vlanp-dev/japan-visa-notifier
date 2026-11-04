import assert from 'node:assert/strict';
import test from 'node:test';
import { REDACTED, createLogger, redact } from '../src/logger.js';

function captureLogger(options = {}) {
  const lines = [];
  const stream = {
    write(chunk) {
      lines.push(String(chunk));
      return true;
    },
  };
  const logger = createLogger({ stream, ...options });
  return { logger, lines };
}

test('log records are JSON lines with time, level and message', () => {
  const { logger, lines } = captureLogger({
    now: () => new Date('2026-11-04T10:00:00.000Z'),
  });
  logger.info('service_started', { port: 3000 });
  assert.equal(lines.length, 1);
  const record = JSON.parse(lines[0]);
  assert.deepEqual(record, {
    time: '2026-11-04T10:00:00.000Z',
    level: 'info',
    msg: 'service_started',
    port: 3000,
  });
});

test('sensitive keys and known secrets are redacted', () => {
  const { logger, lines } = captureLogger({
    secrets: ['re_supersecretkey', 'operator-password'],
  });
  logger.info('send failed with operator-password', {
    password: 'hunter2',
    nested: { apiKey: 're_supersecretkey', note: 'Bearer re_supersecretkey' },
    authorization: 'Basic abc',
    cookie: 'csrf_token=abc',
    visible: 'safe value',
  });
  const output = lines.join('');
  assert.doesNotMatch(output, /hunter2/);
  assert.doesNotMatch(output, /re_supersecretkey/);
  assert.doesNotMatch(output, /operator-password/);
  assert.doesNotMatch(output, /Basic abc/);
  const record = JSON.parse(lines[0]);
  assert.equal(record.password, REDACTED);
  assert.equal(record.nested.apiKey, REDACTED);
  assert.equal(record.nested.note, `Bearer ${REDACTED}`);
  assert.equal(record.visible, 'safe value');
});

test('error objects are serialized without secrets and without stacks', () => {
  const { logger, lines } = captureLogger({ secrets: ['top-secret-value'] });
  const error = new Error('provider rejected top-secret-value');
  error.code = 'PROVIDER_REFUSED';
  logger.error('send_failed', { error });
  const record = JSON.parse(lines[0]);
  assert.deepEqual(record.error, {
    name: 'Error',
    message: `provider rejected ${REDACTED}`,
    code: 'PROVIDER_REFUSED',
  });
  assert.doesNotMatch(lines[0], /stack/);
});

test('level filtering and child bindings work', () => {
  const { logger, lines } = captureLogger({ level: 'warn' });
  logger.debug('ignored');
  logger.info('ignored');
  logger.child({ component: 'http' }).warn('kept', { statusCode: 403 });
  assert.equal(lines.length, 1);
  const record = JSON.parse(lines[0]);
  assert.equal(record.msg, 'kept');
  assert.equal(record.component, 'http');
  assert.equal(record.statusCode, 403);
});

test('redact handles arrays, cycles and depth', () => {
  const value = { list: [{ password: 'a' }] };
  value.self = value;
  const result = redact(value);
  assert.equal(result.list[0].password, REDACTED);
  assert.equal(result.self, '[circular]');
});
