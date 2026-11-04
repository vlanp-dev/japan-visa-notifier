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

test('the service exposes public signed acknowledgement links', async (t) => {
  const dir = mkdtempSync(join(tmpdir(), 'ambassade-alerts-ack-service-'));
  const service = createService({
    env: serviceEnv(dir, { PUBLIC_BASE_URL: 'https://alerts.example.test' }),
  });
  t.after(async () => {
    await service.close();
    rmSync(dir, { recursive: true, force: true });
  });
  const address = await service.listen();
  const baseUrl = `http://127.0.0.1:${address.port}`;

  const invalid = await fetch(`${baseUrl}/ack/not-a-token`);
  assert.equal(invalid.status, 400);
  assert.match(invalid.headers.get('content-type'), /text\/html/);

  const opened = service.episodeService.openEpisode({
    embassyName: 'Ambassade de France',
    embassyPageUrl: 'https://example.test/ambassade',
    excerpt: 'Extrait',
    rules: [{ ruleKey: 'visa', label: 'Section visa modifiée' }],
  });
  const path = new URL(opened.acknowledgementUrl).pathname;

  const preview = await fetch(`${baseUrl}${path}`);
  assert.equal(preview.status, 200);
  assert.match(await preview.text(), /Acquitter l’alerte/);
  assert.equal(service.repositories.episodes.getById(opened.episode.id).status, 'open');

  const acknowledged = await fetch(`${baseUrl}${path}`, { method: 'POST' });
  assert.equal(acknowledged.status, 200);
  assert.equal(service.repositories.episodes.getById(opened.episode.id).status, 'acknowledged');
  assert.equal(typeof service.ackTokenSigner.secret, 'string');
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
