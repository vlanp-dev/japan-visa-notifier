import assert from 'node:assert/strict';
import test from 'node:test';
import { AckTokenSigner } from '../src/ack/ack-token-signer.js';
import { EmailService } from '../src/email/email-service.js';
import { EmailSettingsService } from '../src/email/settings.js';
import { AlertEpisodeService } from '../src/services/alert-episode-service.js';
import { EpisodeService } from '../src/services/episode-service.js';
import { openTestDatabase } from './helpers.js';

const NOW = new Date('2026-11-04T10:00:00.000Z');
const API_KEY = 're_alert_episode_test_key_1234567890';
const ACK_SECRET = 'alert-episode-test-secret-1234567890';
const PUBLIC_BASE_URL = 'https://alerts.example.test';

const EPISODE = Object.freeze({
  embassyName: 'Ambassade de France',
  embassyPageUrl: 'https://example.test/ambassade',
  excerpt: 'Le contenu a changé.',
  rules: [
    { ruleKey: 'visa', label: 'Section visa modifiée' },
    { ruleKey: 'contact', label: 'Contact modifié' },
  ],
});

function setup(t, { withSettings = true } = {}) {
  const context = openTestDatabase(t, { now: () => NOW });
  const emailSettings = new EmailSettingsService({
    repositories: context.repositories,
    apiKeyPath: context.config.resendApiKeyPath,
  });
  if (withSettings) {
    emailSettings.update({
      sender: 'alertes@alertes.vlanp.com',
      recipient: 'operateur@example.test',
      sendingSubdomain: 'alertes',
      apiKey: API_KEY,
    });
  }
  const tokenSigner = new AckTokenSigner({
    secret: ACK_SECRET,
    publicBaseUrl: PUBLIC_BASE_URL,
    now: () => NOW,
  });
  const episodeService = new EpisodeService({
    repositories: context.repositories,
    tokenSigner,
    now: () => NOW,
  });
  const calls = [];
  const emailService = new EmailService({
    repositories: context.repositories,
    emailSettings,
    resendClient: {
      async send(options) {
        calls.push(options);
        return {
          status: 'accepted',
          providerMessageId: 'resend-alert-episode',
          reason: null,
        };
      },
    },
    now: () => NOW,
  });
  const alertEpisodeService = new AlertEpisodeService({
    episodeService,
    emailService,
  });
  return { ...context, alertEpisodeService, episodeService, emailService, calls };
}

test('the open-and-notify use case persists the episode and makes exactly one attempt', async (t) => {
  const context = setup(t);

  const result = await context.alertEpisodeService.openAndNotify(EPISODE);

  assert.equal(result.episode.status, 'open');
  assert.equal(result.episode.rules.length, 2);
  assert.equal(result.delivery.status, 'accepted');
  assert.equal(context.calls.length, 1);
  assert.equal(context.repositories.emailAttempts.list().length, 1);
  assert.equal(context.repositories.emailAttempts.list()[0].episodeId, result.episode.id);
  const claim = context.repositories.emailDeliveryClaims.findByEpisodeId(result.episode.id);
  assert.equal(claim.pending, false);
  assert.equal(claim.outcome, 'accepted');
});

test('the opening transaction leaves a durable delivery intent before any attempt', (t) => {
  const context = setup(t);

  const opened = context.episodeService.openEpisode(EPISODE);

  const claim = context.repositories.emailDeliveryClaims.findByEpisodeId(opened.episode.id);
  assert.ok(claim, 'the episode commit includes its delivery intent');
  assert.equal(claim.pending, true);
  assert.equal(claim.started, false);
  assert.equal(context.calls.length, 0);
  assert.equal(context.repositories.emailAttempts.findByEpisodeId(opened.episode.id), null);
});

test('an incomplete configuration is reported without making the use case throw', async (t) => {
  const context = setup(t, { withSettings: false });

  const result = await context.alertEpisodeService.openAndNotify(EPISODE);

  assert.equal(result.delivery.status, 'not_attempted');
  assert.match(result.delivery.reason, /Configuration email incomplète/);
  assert.equal(context.calls.length, 0);
});
