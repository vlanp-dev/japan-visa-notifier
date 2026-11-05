import { randomUUID } from 'node:crypto';
import { composeAlertEmail, composeTestEmail } from './compose.js';

export const BANNER_REFUSAL_THRESHOLD = 2;
export const MAX_DELIVERY_REASON_LENGTH = 500;

const PENDING_REASON =
  'Envoi déjà réservé pour cet épisode ; aucun nouvel envoi n’a été effectué et le résultat reste indéterminé.';
const INTERNAL_FAILURE_REASON =
  'Envoi non tenté : erreur interne pendant la préparation du message.';
const MISSING_API_KEY_REASON = 'Configuration email invalide : clé API Resend absente.';
const UNREADABLE_API_KEY_REASON =
  'Configuration email invalide : clé API Resend illisible.';
const UNRECORDED_REASON =
  'Envoi effectué mais résultat non enregistré ; l’état reste indéterminé.';
const PROVIDER_WITHOUT_REASON =
  'Envoi refusé par le fournisseur sans motif communiqué.';
const INVALID_PROVIDER_RESPONSE =
  'Réponse invalide du fournisseur email ; l’envoi est considéré comme refusé.';
const REDACTED_SECRET = '[clé masquée]';

function redactSecretText(value, secret) {
  if (typeof secret !== 'string' || secret === '' || typeof value !== 'string') {
    return value;
  }
  return value.split(secret).join(REDACTED_SECRET);
}

function redactedError(error, secret) {
  if (error instanceof Error) {
    return {
      name: error.name,
      message: redactSecretText(error.message, secret),
      ...(typeof error.code === 'string' ? { code: error.code } : {}),
    };
  }
  return { message: redactSecretText(String(error), secret) };
}

function normalizeReason(value, fallback, secret = '') {
  const text = typeof value === 'string' ? value.replace(/\s+/g, ' ').trim() : '';
  const reason = redactSecretText(text === '' ? fallback : text, secret);
  if (reason.length <= MAX_DELIVERY_REASON_LENGTH) {
    return reason;
  }
  return `${reason.slice(0, MAX_DELIVERY_REASON_LENGTH - 3)}...`;
}

function resultFromAttempt(attempt, { duplicate = false } = {}) {
  return {
    kind: attempt.kind,
    episodeId: attempt.episodeId,
    attemptId: attempt.id,
    status: attempt.status,
    providerMessageId: attempt.providerMessageId,
    reason: attempt.reason,
    attemptedAt: attempt.attemptedAt,
    duplicate,
    pending: false,
  };
}

function pendingResult(episodeId, claim, reason = PENDING_REASON) {
  return {
    kind: 'episode',
    episodeId,
    attemptId: null,
    status: null,
    providerMessageId: null,
    reason,
    attemptedAt: null,
    duplicate: true,
    pending: true,
    claimStarted: claim?.started === true,
    claimedAt: claim?.claimedAt ?? null,
  };
}

function notStartedResult(episodeId) {
  return {
    kind: 'episode',
    episodeId,
    attemptId: null,
    status: null,
    providerMessageId: null,
    reason: null,
    attemptedAt: null,
    duplicate: false,
    pending: false,
  };
}

function resolveOpening(input) {
  if (input === null || typeof input !== 'object' || Array.isArray(input)) {
    throw new TypeError('deliverEpisode expects the result of opening an episode.');
  }
  const episode =
    input.episode !== null && typeof input.episode === 'object' ? input.episode : input;
  if (!Number.isInteger(episode.id) || episode.id < 1) {
    throw new TypeError('episode.id doit être un entier positif.');
  }
  const acknowledgementUrl =
    input.acknowledgementUrl ?? episode.acknowledgementUrl ?? null;
  const deliveryClaimToken = input.deliveryClaimToken ?? input.claimToken ?? null;
  if (
    deliveryClaimToken !== null &&
    (typeof deliveryClaimToken !== 'string' || deliveryClaimToken.trim() === '')
  ) {
    throw new TypeError('deliveryClaimToken doit être une chaîne non vide.');
  }
  return { episode, acknowledgementUrl, deliveryClaimToken };
}

/**
 * Single-attempt email delivery for alert episodes.
 *
 * A durable claim is written before the provider call, so at most one provider
 * request is made per episode even when several rules trigger together, two
 * requests race, or the process restarts with an unfinished claim. The final
 * state is exactly one of `accepted`, `refused` or `not_attempted`, and it is
 * always read back from the database.
 */
export class EmailService {
  constructor({
    repositories,
    emailSettings,
    resendClient,
    logger = null,
    now = () => new Date(),
    bannerThreshold = BANNER_REFUSAL_THRESHOLD,
    composeAlert = composeAlertEmail,
    composeTest = composeTestEmail,
  } = {}) {
    if (
      !repositories ||
      !repositories.episodes ||
      !repositories.emailAttempts ||
      !repositories.emailDeliveryClaims
    ) {
      throw new TypeError(
        'repositories with episodes, emailAttempts and emailDeliveryClaims are required.',
      );
    }
    if (!emailSettings || typeof emailSettings.getConfigurationState !== 'function') {
      throw new TypeError('emailSettings with getConfigurationState is required.');
    }
    if (!resendClient || typeof resendClient.send !== 'function') {
      throw new TypeError('resendClient with send is required.');
    }
    if (typeof now !== 'function') {
      throw new TypeError('now must be a function.');
    }
    if (!Number.isInteger(bannerThreshold) || bannerThreshold < 1) {
      throw new TypeError('bannerThreshold must be a positive integer.');
    }
    if (typeof composeAlert !== 'function' || typeof composeTest !== 'function') {
      throw new TypeError('composeAlert and composeTest must be functions.');
    }

    this.episodes = repositories.episodes;
    this.emailAttempts = repositories.emailAttempts;
    this.claims = repositories.emailDeliveryClaims;
    this.emailSettings = emailSettings;
    this.resendClient = resendClient;
    this.logger = logger;
    this.now = now;
    this.bannerThreshold = bannerThreshold;
    this.composeAlert = composeAlert;
    this.composeTest = composeTest;
    /** Process-local coalescing; the database claim is the durable backstop. */
    this.inFlight = new Map();
  }

  /**
   * Deliver the alert email for one opened episode. Concurrent calls for the
   * same episode share one in-flight promise. Returns the persisted outcome, or
   * an explicit indeterminate result when another writer holds an unfinished
   * claim. It never throws for delivery or configuration failures.
   */
  async deliverEpisode(opening = {}) {
    const { episode, acknowledgementUrl, deliveryClaimToken } = resolveOpening(opening);
    const episodeId = episode.id;

    const existing = this.emailAttempts.findByEpisodeId(episodeId);
    if (existing !== null) {
      return resultFromAttempt(existing, { duplicate: true });
    }

    const running = this.inFlight.get(episodeId);
    if (running !== undefined) {
      return running;
    }

    const promise = this.#deliverEpisodeOnce({
      episode,
      acknowledgementUrl,
      deliveryClaimToken,
    });
    this.inFlight.set(episodeId, promise);
    try {
      return await promise;
    } finally {
      if (this.inFlight.get(episodeId) === promise) {
        this.inFlight.delete(episodeId);
      }
    }
  }

  /**
   * Acquire the single provider-send slot for an episode. When the opening
   * transaction already wrote an unstarted intent, `start` atomically claims it
   * so only one caller — across processes — can contact the provider.
   */
  #acquireClaim(episodeId, deliveryClaimToken) {
    if (deliveryClaimToken === null) {
      return this.claims.claim(episodeId, { claimToken: randomUUID() });
    }
    const existing = this.claims.findByEpisodeId(episodeId);
    if (existing === null) {
      return this.claims.claim(episodeId, { claimToken: deliveryClaimToken });
    }
    if (existing.claimToken !== deliveryClaimToken || existing.completedAt !== null) {
      return { claimed: false, claim: existing };
    }
    const started = this.claims.start(episodeId, { claimToken: deliveryClaimToken });
    return { claimed: started.started, claim: started.claim };
  }

  async #deliverEpisodeOnce({ episode, acknowledgementUrl, deliveryClaimToken }) {
    const episodeId = episode.id;
    const claimToken = deliveryClaimToken ?? randomUUID();
    const claimed = this.#acquireClaim(episodeId, deliveryClaimToken);

    if (!claimed.claimed) {
      const existing = this.emailAttempts.findByEpisodeId(episodeId);
      if (existing !== null) {
        return resultFromAttempt(existing, { duplicate: true });
      }
      return pendingResult(episodeId, claimed.claim);
    }

    let outcome;
    try {
      outcome = await this.#prepareAndSend({ episode, acknowledgementUrl });
    } catch (error) {
      this.#log('email_delivery_preparation_failure', { episodeId, error });
      outcome = {
        status: 'not_attempted',
        providerMessageId: null,
        reason: INTERNAL_FAILURE_REASON,
      };
    }

    try {
      const recorded = this.claims.complete({
        episodeId,
        claimToken,
        status: outcome.status,
        providerMessageId: outcome.providerMessageId,
        reason: outcome.reason,
      });
      return resultFromAttempt(recorded.attempt, { duplicate: !recorded.created });
    } catch (error) {
      this.#log('email_delivery_record_failure', { episodeId, error });
      const existing = this.emailAttempts.findByEpisodeId(episodeId);
      if (existing !== null) {
        return resultFromAttempt(existing, { duplicate: true });
      }
      return pendingResult(episodeId, this.claims.findByEpisodeId(episodeId), UNRECORDED_REASON);
    }
  }

  /** Send the separate French configuration-test email (kind `test`). */
  async sendTestEmail() {
    let outcome;
    try {
      outcome = await this.#prepareAndSendTest();
    } catch (error) {
      this.#log('email_test_preparation_failure', { error });
      outcome = {
        status: 'not_attempted',
        providerMessageId: null,
        reason: INTERNAL_FAILURE_REASON,
      };
    }

    try {
      const attempt = this.emailAttempts.create({
        kind: 'test',
        status: outcome.status,
        providerMessageId: outcome.providerMessageId,
        reason: outcome.reason,
      });
      return resultFromAttempt(attempt, { duplicate: false });
    } catch (error) {
      this.#log('email_test_record_failure', { error });
      return {
        kind: 'test',
        episodeId: null,
        attemptId: null,
        status: null,
        providerMessageId: null,
        reason: UNRECORDED_REASON,
        attemptedAt: null,
        duplicate: false,
        pending: true,
      };
    }
  }

  /**
   * Banner state derived only from persisted attempts: refused sends recorded
   * after the most recent accepted send. `not_attempted` rows are ignored.
   */
  getBannerState() {
    const refusedCount = this.emailAttempts.countRefusedSinceLastAccepted();
    const visible = refusedCount >= this.bannerThreshold;
    return {
      visible,
      refusedCount,
      threshold: this.bannerThreshold,
      message: visible
        ? 'Des envois d’alerte par email ont échoué de façon répétée. Vérifiez la configuration email et les motifs enregistrés.'
        : null,
    };
  }

  /**
   * Read-only delivery state for one episode, including the indeterminate
   * state left by a durable claim without a persisted attempt.
   */
  getDeliveryState(episodeId) {
    if (!Number.isInteger(episodeId) || episodeId < 1) {
      throw new TypeError('episodeId doit être un entier positif.');
    }
    const attempt = this.emailAttempts.findByEpisodeId(episodeId);
    if (attempt !== null) {
      return resultFromAttempt(attempt, { duplicate: false });
    }
    const claim = this.claims.findByEpisodeId(episodeId);
    if (claim !== null) {
      return pendingResult(episodeId, claim);
    }
    return notStartedResult(episodeId);
  }

  async #prepareAndSend({ episode, acknowledgementUrl }) {
    const prepared = this.#prepareConfiguration();
    if (prepared.failure !== null) {
      return prepared.failure;
    }
    const message = this.composeAlert({ episode, acknowledgementUrl });
    return this.#safeSend({
      apiKey: prepared.apiKey,
      from: prepared.configuration.sender,
      to: prepared.configuration.recipient,
      subject: message.subject,
      html: message.html,
      text: message.text,
      idempotencyKey: `episode-${episode.id}`,
    });
  }

  async #prepareAndSendTest() {
    const prepared = this.#prepareConfiguration();
    if (prepared.failure !== null) {
      return prepared.failure;
    }
    const message = this.composeTest();
    return this.#safeSend({
      apiKey: prepared.apiKey,
      from: prepared.configuration.sender,
      to: prepared.configuration.recipient,
      subject: message.subject,
      html: message.html,
      text: message.text,
      idempotencyKey: `test-${randomUUID()}`,
    });
  }

  /**
   * Report a configuration failure without contacting the provider, and read
   * the API key from its private file for this send only.
   */
  #prepareConfiguration() {
    const configuration = this.emailSettings.getConfigurationState();
    if (!configuration.complete) {
      const missing = Array.isArray(configuration.missing) ? configuration.missing : [];
      const invalid = Array.isArray(configuration.invalid) ? configuration.invalid : [];
      let reason;
      if (typeof configuration.apiKeyError === 'string' && configuration.apiKeyError !== '') {
        reason = `Configuration email invalide : ${configuration.apiKeyError}`;
      } else if (missing.length > 0) {
        reason = `Configuration email incomplète : ${missing.join(', ')}.`;
        if (invalid.length > 0) {
          reason += ` Champs invalides : ${invalid.join(', ')}.`;
        }
      } else {
        reason = `Configuration email invalide : champs invalides : ${invalid.join(', ')}.`;
      }
      return {
        configuration,
        apiKey: null,
        failure: {
          status: 'not_attempted',
          providerMessageId: null,
          reason: normalizeReason(reason, 'Configuration email incomplète.'),
        },
      };
    }

    let apiKey;
    try {
      apiKey = this.emailSettings.readApiKey();
    } catch {
      return {
        configuration,
        apiKey: null,
        failure: {
          status: 'not_attempted',
          providerMessageId: null,
          reason: UNREADABLE_API_KEY_REASON,
        },
      };
    }
    if (typeof apiKey !== 'string' || apiKey === '') {
      return {
        configuration,
        apiKey: null,
        failure: {
          status: 'not_attempted',
          providerMessageId: null,
          reason: MISSING_API_KEY_REASON,
        },
      };
    }
    return { configuration, apiKey, failure: null };
  }

  /** Never let a provider or transport failure escape the delivery path. */
  async #safeSend(options) {
    const secret = typeof options.apiKey === 'string' ? options.apiKey : '';
    let result;
    try {
      result = await this.resendClient.send(options);
    } catch (error) {
      this.#log('email_provider_failure', { error: redactedError(error, secret) });
      return {
        status: 'refused',
        providerMessageId: null,
        reason: 'Erreur inattendue lors de l’appel au fournisseur email.',
      };
    }

    if (result === null || typeof result !== 'object' || Array.isArray(result)) {
      return {
        status: 'refused',
        providerMessageId: null,
        reason: INVALID_PROVIDER_RESPONSE,
      };
    }

    if (
      result.status === 'accepted' &&
      typeof result.providerMessageId === 'string' &&
      result.providerMessageId.trim() !== ''
    ) {
      return {
        status: 'accepted',
        providerMessageId: redactSecretText(result.providerMessageId.trim(), secret),
        reason: null,
      };
    }

    if (result.status === 'accepted') {
      return {
        status: 'refused',
        providerMessageId: null,
        reason: INVALID_PROVIDER_RESPONSE,
      };
    }

    return {
      status: 'refused',
      providerMessageId: null,
      reason: normalizeReason(result.reason, PROVIDER_WITHOUT_REASON, secret),
    };
  }

  #log(event, fields) {
    if (!this.logger || typeof this.logger.warn !== 'function') {
      return;
    }
    try {
      this.logger.warn(event, fields);
    } catch {
      // Logging must never take the delivery path down.
    }
  }
}

export function createEmailService(options) {
  return new EmailService(options);
}
