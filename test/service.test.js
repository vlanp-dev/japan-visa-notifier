import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { createService } from '../src/server.js';
import { baseTestEnv } from './helpers.js';

function serviceEnv(dir, overrides = {}) {
  return baseTestEnv(dir, { LOG_LEVEL: 'silent', ...overrides });
}

test('the service starts and stays healthy without any email configuration', async (t) => {
  const dir = mkdtempSync(join(tmpdir(), 'ambassade-alerts-service-'));
  const service = createService({ env: serviceEnv(dir) });
  t.after(async () => {
    await service.close();
    rmSync(dir, { recursive: true, force: true });
  });

  const address = await service.listen();
  const response = await fetch(`http://127.0.0.1:${address.port}/health`);
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { status: 'ok', database: 'ok' });

  assert.equal(service.config.appEnv, 'test');
  assert.equal('email' in service.config, false);
  assert.equal(service.repositories.settings.get('sender'), null);
});

test('settings and schema survive a service restart', async (t) => {
  const dir = mkdtempSync(join(tmpdir(), 'ambassade-alerts-restart-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));

  const first = createService({ env: serviceEnv(dir) });
  await first.listen();
  first.repositories.settings.set('sender', 'alertes@vlanp.com');
  first.repositories.appSecrets.set('ack', 'stable-secret');
  await first.close();

  const second = createService({ env: serviceEnv(dir) });
  t.after(() => second.close());
  const address = await second.listen();
  const response = await fetch(`http://127.0.0.1:${address.port}/health`);
  assert.equal(response.status, 200);
  assert.equal(second.repositories.settings.get('sender'), 'alertes@vlanp.com');
  assert.equal(second.repositories.appSecrets.get('ack'), 'stable-secret');
});
