import assert from 'node:assert/strict';
import test from 'node:test';
import { AckTokenSigner } from '../src/ack/ack-token-signer.js';
import { openDatabase } from '../src/db.js';
import { EmailService } from '../src/email/email-service.js';
import { createResendClient } from '../src/email/resend-client.js';
import { EmailSettingsService } from '../src/email/settings.js';
import { createLogger } from '../src/logger.js';
import { createRepositories } from '../src/repositories/index.js';
import { EpisodeService } from '../src/services/episode-service.js';
import { openTestDatabase } from './helpers.js';

const NOW = new Date('2026-11-04T10:00:00.000Z');
const API_KEY = 're_test_delivery_secret_1234567890';
const ACK_SECRET = 'ack-secret-for-delivery-tests-1234567890';
const PUBLIC_BASE_URL = 'https://alerts.example.test';

const SETTINGS = Object.freeze({
  sender: 'alertes@alertes.vlanp.com',
  recipient: 'operateur@example.test',
  sendingSubdomain: 'alertes',
  apiKey: API_KEY,
});

const EPISODE = Object.freeze({
  embassyName: 'Ambassade de France',
  embassyPageUrl: 'https://example.test/ambassade',
  excerpt: 'Le contenu <modifié> & nouveau.',
  rules: [
    { ruleKey: 'visa', label: 'Section visa modifiée', detail: 'délai modifié' },
    { ruleKey: 'contact', label: 'Contact modifié' },
  ],
});

function accepted(providerMessageId = 'resend-accepted-1') {
  return { status: 'accepted', providerMessageId, reason: null };
}

function refused(reason) {
  return { status: 'refused', providerMessageId: null, reason };
}

function captureLogger() {
  const lines = [];
  const stream = {
    write(chunk) {
      lines.push(String(chunk));
      return true;
    },
  };
  return {
    lines,
    logger: createLogger({
      level: 'debug',
      stream,
      errorStream: stream,
      secrets: [API_KEY],
    }),
  };
}

function createFakeClient(handler = () => accepted()) {
  const calls = [];
  return {
    calls,
    async send(options) {
      calls.push(options);
      return handler(options, calls.length);
    },
  };
}

function delay(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

function createDeliveryContext(
  t,
  { withSettings = true, handler, clientFactory, now = () => NOW } = {},
) {
  const { db, config, repositories, dir } = openTestDatabase(t, { now });
  const emailSettings = new EmailSettingsService({
    repositories,
    apiKeyPath: config.resendApiKeyPath,
  });
  if (withSettings) {
    emailSettings.update(SETTINGS);
  }
  const tokenSigner = new AckTokenSigner({
    secret: ACK_SECRET,
    publicBaseUrl: PUBLIC_BASE_URL,
    now,
  });
  const episodeService = new EpisodeService({ repositories, tokenSigner, now });
  const captured = captureLogger();
  const client = clientFactory
    ? clientFactory({ repositories, emailSettings, logger: captured.logger, dir, config })
    : createFakeClient(handler);
  const emailService = new EmailService({
    repositories,
    emailSettings,
    resendClient: client,
    logger: captured.logger,
    now,
  });
  return {
    db,
    config,
    repositories,
    dir,
    emailSettings,
    tokenSigner,
    episodeService,
    client,
    captured,
    emailService,
  };
}

function openEpisode(context, overrides = {}) {
  return context.episodeService.openEpisode({ ...EPISODE, ...overrides });
}

function episodeAttempts(context) {
  return context.repositories.emailAttempts.list({ limit: 500 }).filter(
    (attempt) => attempt.kind === 'episode',
  );
}

test('one episode with two rules makes one accepted send containing every required part', async (t) => {
  const context = createDeliveryContext(t);
  const opened = openEpisode(context);

  const result = await context.emailService.deliverEpisode(opened);

  assert.equal(result.status, 'accepted');
  assert.equal(result.duplicate, false);
  assert.equal(result.pending, false);
  assert.equal(result.providerMessageId, 'resend-accepted-1');
  assert.equal(result.reason, null);
  assert.equal(context.client.calls.length, 1);

  const call = context.client.calls[0];
  assert.equal(call.from, SETTINGS.sender);
  assert.equal(call.to, SETTINGS.recipient);
  assert.equal(call.apiKey, API_KEY);
  assert.equal(call.idempotencyKey, `episode-${opened.episode.id}`);
  assert.match(call.html, /Section visa modifiée/);
  assert.match(call.html, /Contact modifié/);
  assert.match(call.html, /Le contenu &lt;modifié&gt; &amp; nouveau\./);
  assert.ok(call.html.includes(opened.acknowledgementUrl));
  assert.ok(call.text.includes(opened.acknowledgementUrl));
  assert.match(call.text, /https:\/\/example\.test\/ambassade/);

  const stored = context.repositories.emailAttempts.findByEpisodeId(opened.episode.id);
  assert.equal(stored.status, 'accepted');
  assert.equal(stored.providerMessageId, 'resend-accepted-1');
  assert.equal(stored.reason, null);
  assert.equal(result.attemptId, stored.id);

  const claim = context.repositories.emailDeliveryClaims.findByEpisodeId(opened.episode.id);
  assert.equal(claim.pending, false);
  assert.equal(claim.outcome, 'accepted');

  for (const forbidden of ['délivrabilité', 'délivré', 'ouverture', 'cliqué', 'clics', 'tracking']) {
    assert.ok(!call.html.toLowerCase().includes(forbidden), forbidden);
    assert.ok(!call.text.toLowerCase().includes(forbidden), forbidden);
  }
  const exported = JSON.stringify(result);
  assert.ok(!exported.includes(API_KEY));
  assert.ok(!context.captured.lines.join('\n').includes(API_KEY));
  assert.ok(!JSON.stringify(stored).includes(API_KEY));
});

test('concurrent deliveries in one service instance share one provider request', async (t) => {
  const context = createDeliveryContext(t, {
    handler: async () => {
      await delay(25);
      return accepted('resend-single-flight');
    },
  });
  const opened = openEpisode(context);

  const [first, second] = await Promise.all([
    context.emailService.deliverEpisode(opened),
    context.emailService.deliverEpisode(opened),
  ]);

  assert.equal(context.client.calls.length, 1);
  assert.equal(first.status, 'accepted');
  assert.equal(second.status, 'accepted');
  assert.equal(episodeAttempts(context).length, 1);
});

test('missing configuration records not_attempted without calling the provider', async (t) => {
  const context = createDeliveryContext(t, { withSettings: false });
  const opened = openEpisode(context);

  const result = await context.emailService.deliverEpisode(opened);

  assert.equal(result.status, 'not_attempted');
  assert.equal(result.pending, false);
  assert.match(result.reason, /Configuration email incomplète/);
  assert.match(result.reason, /expéditeur/);
  assert.equal(context.client.calls.length, 0);

  const stored = context.repositories.emailAttempts.findByEpisodeId(opened.episode.id);
  assert.equal(stored.status, 'not_attempted');
  assert.equal(stored.reason, result.reason);

  const again = await context.emailService.deliverEpisode(opened);
  assert.equal(again.status, 'not_attempted');
  assert.equal(again.duplicate, true);
  assert.equal(context.client.calls.length, 0);
  assert.equal(episodeAttempts(context).length, 1);
});

test('an invalid key is refused with the redacted provider reason and the service keeps working', async (t) => {
  let providerCalls = 0;
  const context = createDeliveryContext(t, {
    clientFactory: ({ logger }) =>
      createResendClient({
        logger,
        fetchImpl: async () => {
          providerCalls += 1;
          if (providerCalls === 1) {
            return new Response(
              JSON.stringify({
                name: 'validation_error',
                message: `API key is invalid: ${API_KEY}`,
              }),
              { status: 401, headers: { 'content-type': 'application/json' } },
            );
          }
          return new Response(JSON.stringify({ id: 'resend-after-refusal' }), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          });
        },
      }),
  });

  const refusedResult = await context.emailService.deliverEpisode(openEpisode(context));
  assert.equal(refusedResult.status, 'refused');
  assert.equal(refusedResult.providerMessageId, null);
  assert.match(refusedResult.reason, /HTTP 401/);
  assert.match(refusedResult.reason, /API key is invalid/);
  assert.ok(refusedResult.reason.includes('[clé masquée]'));
  assert.ok(!refusedResult.reason.includes(API_KEY));
  assert.ok(!context.captured.lines.join('\n').includes(API_KEY));

  const stored = context.repositories.emailAttempts.list({ limit: 10 })[0];
  assert.equal(stored.status, 'refused');
  assert.equal(stored.reason, refusedResult.reason);

  const acceptedResult = await context.emailService.deliverEpisode(openEpisode(context));
  assert.equal(acceptedResult.status, 'accepted');
  assert.equal(acceptedResult.providerMessageId, 'resend-after-refusal');
  assert.equal(providerCalls, 2);
});

test('the service redacts the API key echoed by a custom provider client', async (t) => {
  const context = createDeliveryContext(t, {
    handler: () => refused(`clé refusée : ${API_KEY}`),
  });

  const result = await context.emailService.deliverEpisode(openEpisode(context));

  assert.equal(result.status, 'refused');
  assert.ok(result.reason.includes('[clé masquée]'));
  assert.ok(!result.reason.includes(API_KEY));
  assert.ok(!context.captured.lines.join('\n').includes(API_KEY));
});

test('an invalid sender is recorded as not attempted and does not stop later sends', async (t) => {
  const context = createDeliveryContext(t, {
    handler: (_options, index) =>
      index === 1 ? accepted('resend-after-invalid-sender') : accepted('resend-later'),
  });
  context.repositories.settings.set('email.sender', 'adresse-sans-domaine');

  const first = await context.emailService.deliverEpisode(openEpisode(context));
  assert.equal(first.status, 'not_attempted');
  assert.match(first.reason, /Configuration email invalide/);
  assert.match(first.reason, /expéditeur/);
  assert.equal(context.client.calls.length, 0);
  assert.equal(
    context.repositories.emailAttempts.list({ limit: 10 })[0].reason,
    first.reason,
  );

  context.repositories.settings.set('email.sender', SETTINGS.sender);
  const second = await context.emailService.deliverEpisode(openEpisode(context));
  assert.equal(second.status, 'accepted');
  assert.equal(context.client.calls.length, 1);
});

test('a network failure is recorded as refused and never rejects the call', async (t) => {
  const context = createDeliveryContext(t, {
    clientFactory: ({ logger }) =>
      createResendClient({
        logger,
        fetchImpl: async () => {
          throw new TypeError('fetch failed');
        },
      }),
  });

  const result = await context.emailService.deliverEpisode(openEpisode(context));
  assert.equal(result.status, 'refused');
  assert.match(result.reason, /Erreur réseau/);
  const stored = context.repositories.emailAttempts.list({ limit: 10 })[0];
  assert.equal(stored.status, 'refused');
  assert.match(stored.reason, /Erreur réseau/);

  const second = await context.emailService.deliverEpisode(openEpisode(context));
  assert.equal(second.status, 'refused');
});

test('two service instances over the same database make a single provider request', async (t) => {
  const { db, config, repositories } = openTestDatabase(t);
  const secondDb = openDatabase(config.databasePath);
  t.after(() => {
    try {
      secondDb.close();
    } catch {
      // Already closed.
    }
  });
  const secondRepositories = createRepositories(secondDb);

  const firstSettings = new EmailSettingsService({
    repositories,
    apiKeyPath: config.resendApiKeyPath,
  });
  firstSettings.update(SETTINGS);
  const secondSettings = new EmailSettingsService({
    repositories: secondRepositories,
    apiKeyPath: config.resendApiKeyPath,
  });

  const tokenSigner = new AckTokenSigner({
    secret: ACK_SECRET,
    publicBaseUrl: PUBLIC_BASE_URL,
  });
  const episodeService = new EpisodeService({ repositories, tokenSigner });
  const opened = episodeService.openEpisode(EPISODE);

  const calls = [];
  const sharedClient = {
    async send(options) {
      calls.push(options);
      await delay(30);
      return accepted('resend-cross-instance');
    },
  };
  const firstService = new EmailService({
    repositories,
    emailSettings: firstSettings,
    resendClient: sharedClient,
  });
  const secondService = new EmailService({
    repositories: secondRepositories,
    emailSettings: secondSettings,
    resendClient: sharedClient,
  });

  const [first, second] = await Promise.all([
    firstService.deliverEpisode(opened),
    secondService.deliverEpisode(opened),
  ]);

  assert.equal(calls.length, 1);
  const statuses = [first.status, second.status];
  assert.equal(statuses.filter((status) => status === 'accepted').length, 1);
  assert.equal(statuses.filter((status) => status === null).length, 1);
  const pending = [first, second].find((result) => result.pending);
  assert.ok(pending, 'one caller must observe the indeterminate claim state');
  assert.equal(pending.status, null);
  assert.equal(pending.duplicate, true);
  assert.match(pending.reason, /indéterminé/);

  const attempts = episodeAttempts({ repositories });
  assert.equal(attempts.length, 1);
  assert.equal(attempts[0].status, 'accepted');

  const later = await firstService.deliverEpisode(opened);
  assert.equal(later.status, 'accepted');
  assert.equal(later.duplicate, true);
  assert.equal(calls.length, 1);

  secondDb.close();
});

test('a claim started by a crashed process blocks another provider call and stays indeterminate', async (t) => {
  const context = createDeliveryContext(t);
  const opened = openEpisode(context);
  // Simulate a process that opened the episode, started its claim and then
  // crashed before recording an attempt.
  const started = context.repositories.emailDeliveryClaims.start(opened.episode.id, {
    claimToken: opened.deliveryClaimToken,
  });
  assert.equal(started.started, true);

  const result = await context.emailService.deliverEpisode(opened);

  assert.equal(context.client.calls.length, 0);
  assert.equal(result.status, null);
  assert.equal(result.pending, true);
  assert.equal(result.duplicate, true);
  assert.match(result.reason, /indéterminé/);
  assert.equal(context.repositories.emailAttempts.findByEpisodeId(opened.episode.id), null);

  const state = context.emailService.getDeliveryState(opened.episode.id);
  assert.equal(state.pending, true);
  assert.equal(state.status, null);

  // A claim committed with the episode but never started is also reported as
  // pending, not as an absent attempt.
  const neverStarted = openEpisode(context);
  const unstartedState = context.emailService.getDeliveryState(neverStarted.episode.id);
  assert.equal(unstartedState.pending, true);
  assert.equal(unstartedState.status, null);
  assert.equal(context.client.calls.length, 0);
});

test('the returned outcome is the persisted row when another writer wins the insert', async (t) => {
  let repositories = null;
  const context = createDeliveryContext(t, {
    handler: () => {
      repositories.emailAttempts.create({
        episodeId: opened.episode.id,
        status: 'refused',
        reason: 'HTTP 401 : clé invalide (écrit concurrent)',
      });
      return accepted('resend-late-accepted');
    },
  });
  repositories = context.repositories;
  const opened = openEpisode(context);

  const result = await context.emailService.deliverEpisode(opened);

  assert.equal(result.status, 'refused');
  assert.equal(result.reason, 'HTTP 401 : clé invalide (écrit concurrent)');
  assert.equal(result.providerMessageId, null);
  assert.equal(result.duplicate, true);
  assert.equal(context.client.calls.length, 1);

  const stored = context.repositories.emailAttempts.findByEpisodeId(opened.episode.id);
  assert.equal(stored.status, 'refused');
  assert.equal(stored.reason, result.reason);
  const claim = context.repositories.emailDeliveryClaims.findByEpisodeId(opened.episode.id);
  assert.equal(claim.outcome, 'refused');
});

test('banner appears at two refusals, ignores not_attempted, clears on acceptance and survives restart', async (t) => {
  const outcomes = [
    refused('refus 1'),
    refused('refus 2'),
    accepted('resend-banner-ok'),
    refused('refus 3'),
    refused('refus 4'),
  ];
  const context = createDeliveryContext(t, {
    handler: (options, index) => outcomes[index - 1] ?? refused('refus supplémentaire'),
  });

  assert.deepEqual(context.emailService.getBannerState(), {
    visible: false,
    refusedCount: 0,
    threshold: 2,
    message: null,
  });

  await context.emailService.deliverEpisode(openEpisode(context));
  assert.equal(context.emailService.getBannerState().visible, false);
  assert.equal(context.emailService.getBannerState().refusedCount, 1);

  await context.emailService.deliverEpisode(openEpisode(context));
  const twoRefusals = context.emailService.getBannerState();
  assert.equal(twoRefusals.visible, true);
  assert.equal(twoRefusals.refusedCount, 2);
  assert.match(twoRefusals.message, /échoué de façon répétée/);
  assert.ok(!twoRefusals.message.toLowerCase().includes('délivrabilité'));

  const skipped = openEpisode(context);
  context.repositories.emailAttempts.create({
    episodeId: skipped.episode.id,
    status: 'not_attempted',
    reason: 'Configuration absente',
  });
  assert.equal(context.emailService.getBannerState().visible, true);
  assert.equal(context.emailService.getBannerState().refusedCount, 2);

  const restarted = new EmailService({
    repositories: context.repositories,
    emailSettings: context.emailSettings,
    resendClient: context.client,
    logger: context.captured.logger,
    now: () => NOW,
  });
  assert.equal(restarted.getBannerState().visible, true);
  assert.equal(restarted.getBannerState().refusedCount, 2);

  await restarted.deliverEpisode(openEpisode(context));
  assert.equal(restarted.getBannerState().visible, false);
  assert.equal(restarted.getBannerState().refusedCount, 0);

  await restarted.deliverEpisode(openEpisode(context));
  assert.equal(restarted.getBannerState().visible, false);
  assert.equal(restarted.getBannerState().refusedCount, 1);

  await restarted.deliverEpisode(openEpisode(context));
  assert.equal(restarted.getBannerState().visible, true);
  assert.equal(restarted.getBannerState().refusedCount, 2);
});

test('sendTestEmail records an accepted test attempt without an episode', async (t) => {
  const context = createDeliveryContext(t);

  const result = await context.emailService.sendTestEmail();

  assert.equal(result.kind, 'test');
  assert.equal(result.status, 'accepted');
  assert.equal(result.episodeId, null);
  assert.equal(result.providerMessageId, 'resend-accepted-1');
  assert.equal(context.client.calls.length, 1);
  assert.match(context.client.calls[0].html, /Test de configuration/);

  const stored = context.repositories.emailAttempts.list({ limit: 10 })[0];
  assert.equal(stored.kind, 'test');
  assert.equal(stored.episodeId, null);
  assert.equal(stored.status, 'accepted');
});

test('sendTestEmail reports not_attempted without a provider call when configuration is missing', async (t) => {
  const context = createDeliveryContext(t, { withSettings: false });

  const result = await context.emailService.sendTestEmail();

  assert.equal(result.kind, 'test');
  assert.equal(result.status, 'not_attempted');
  assert.equal(result.pending, false);
  assert.match(result.reason, /Configuration email incomplète/);
  assert.equal(context.client.calls.length, 0);

  const stored = context.repositories.emailAttempts.list({ limit: 10 })[0];
  assert.equal(stored.kind, 'test');
  assert.equal(stored.status, 'not_attempted');
  assert.equal(stored.reason, result.reason);
});
