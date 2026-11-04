import { randomUUID } from 'node:crypto';

export const RESEND_EMAILS_ENDPOINT = 'https://api.resend.com/emails';
export const DEFAULT_TIMEOUT_MS = 10_000;
export const MAX_IDEMPOTENCY_KEY_LENGTH = 256;
export const MAX_REASON_LENGTH = 500;
export const REDACTED_SECRET = '[clé masquée]';

const MAX_RESPONSE_BODY_LENGTH = 64 * 1024;
const MAX_PROVIDER_NAME_LENGTH = 100;

function normalizeSingleLine(value) {
  return String(value).replace(/\s+/g, ' ').trim();
}

function truncate(value, maxLength) {
  if (value.length <= maxLength) {
    return value;
  }
  return `${value.slice(0, Math.max(0, maxLength - 3))}...`;
}

function redactSecrets(value, secrets) {
  let result = value;
  for (const secret of secrets) {
    if (typeof secret === 'string' && secret !== '') {
      result = result.split(secret).join(REDACTED_SECRET);
    }
  }
  return result;
}

function makeReason(value, secrets) {
  return truncate(redactSecrets(normalizeSingleLine(value), secrets), MAX_REASON_LENGTH);
}

function stringifyProviderMessage(message) {
  if (message === undefined || message === null) {
    return null;
  }
  if (typeof message === 'string') {
    return message;
  }
  if (Array.isArray(message)) {
    return message.map((item) => stringifyProviderMessage(item)).filter(Boolean).join('; ');
  }
  if (typeof message === 'object') {
    try {
      return JSON.stringify(message);
    } catch {
      return null;
    }
  }
  return String(message);
}

function describeError(error) {
  if (error instanceof Error) {
    const message = error.message || error.name;
    const cause = error.cause;
    const causeMessage =
      cause instanceof Error ? cause.message : typeof cause === 'string' ? cause : null;
    if (causeMessage && causeMessage !== message) {
      return `${message} (${causeMessage})`;
    }
    return message;
  }
  return String(error);
}

function isTimeoutError(error, signal) {
  if (signal !== undefined && signal.aborted) {
    return true;
  }
  return error?.name === 'TimeoutError';
}

/**
 * Create a Resend email client. Transport failures, provider refusals and
 * malformed provider responses are returned as `refused` results; this client
 * never throws for a send attempt. Constructor misuse still throws a
 * TypeError because it is a programming error.
 *
 * The API key may be stored on the client or supplied per call. It is never
 * included in a result, a reason, or a log record.
 */
export function createResendClient({
  apiKey = '',
  fetchImpl = globalThis.fetch,
  endpoint = RESEND_EMAILS_ENDPOINT,
  timeoutMs = DEFAULT_TIMEOUT_MS,
  logger = null,
} = {}) {
  if (typeof apiKey !== 'string') {
    throw new TypeError('apiKey must be a string.');
  }
  if (typeof fetchImpl !== 'function') {
    throw new TypeError('fetchImpl must be a function.');
  }
  if (typeof endpoint !== 'string' || endpoint === '') {
    throw new TypeError('endpoint must be a non-empty string.');
  }
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1) {
    throw new TypeError('timeoutMs must be a positive integer.');
  }

  const configuredApiKey = apiKey.trim();
  const log =
    logger && typeof logger.child === 'function'
      ? logger.child({ component: 'resend' })
      : logger;

  function logRefused(record) {
    if (!log || typeof log.warn !== 'function') {
      return;
    }
    try {
      log.warn('resend_email_refused', {
        httpStatus: record.httpStatus,
        providerName: record.providerName,
        reason: record.reason,
      });
    } catch {
      // Logging must never take the delivery path down.
    }
  }

  function validateMessage({ from, to, subject, html, text, idempotencyKey }, key) {
    if (key === '') {
      return 'Configuration manquante : clé API Resend absente.';
    }
    if (typeof from !== 'string' || from.trim() === '') {
      return 'Message invalide : expéditeur manquant.';
    }
    if (typeof to !== 'string' || to.trim() === '') {
      return 'Message invalide : destinataire unique manquant.';
    }
    if (typeof subject !== 'string' || subject.trim() === '') {
      return 'Message invalide : objet manquant.';
    }
    if (typeof html !== 'string' || html.trim() === '') {
      return 'Message invalide : corps HTML manquant.';
    }
    if (typeof text !== 'string' || text.trim() === '') {
      return 'Message invalide : corps texte manquant.';
    }
    if (idempotencyKey !== undefined) {
      const trimmed = typeof idempotencyKey === 'string' ? idempotencyKey.trim() : '';
      if (trimmed === '' || trimmed.length > MAX_IDEMPOTENCY_KEY_LENGTH) {
        return `Message invalide : clé d’idempotence absente ou supérieure à ${MAX_IDEMPOTENCY_KEY_LENGTH} caractères.`;
      }
    }
    return null;
  }

  async function readResponseBody(response) {
    if (!response || typeof response.text !== 'function') {
      return '';
    }
    try {
      const body = await response.text();
      return typeof body === 'string' ? body.slice(0, MAX_RESPONSE_BODY_LENGTH) : '';
    } catch {
      return '';
    }
  }

  function parseJson(body) {
    if (body.trim() === '') {
      return null;
    }
    try {
      return JSON.parse(body);
    } catch {
      return null;
    }
  }

  async function send(options = {}) {
    const input = options === null || typeof options !== 'object' ? {} : options;
    const { from, to, subject, html, text, idempotencyKey } = input;
    const key =
      typeof input.apiKey === 'string' ? input.apiKey.trim() : configuredApiKey;
    const secrets = [...new Set([configuredApiKey, key])]
      .filter((secret) => secret !== '')
      .sort((left, right) => right.length - left.length);

    function refused(reason, { httpStatus = null, providerName = null } = {}) {
      const record = {
        status: 'refused',
        httpStatus,
        providerMessageId: null,
        providerName:
          providerName === null
            ? null
            : truncate(
                redactSecrets(normalizeSingleLine(providerName), secrets),
                MAX_PROVIDER_NAME_LENGTH,
              ),
        reason: makeReason(reason, secrets),
      };
      logRefused(record);
      return record;
    }

    const messageError = validateMessage(input, key);
    if (messageError !== null) {
      return refused(messageError);
    }

    const idempotency = idempotencyKey === undefined ? randomUUID() : idempotencyKey.trim();
    const signal = AbortSignal.timeout(timeoutMs);

    let response;
    try {
      response = await fetchImpl(endpoint, {
        method: 'POST',
        redirect: 'error',
        signal,
        headers: {
          Authorization: `Bearer ${key}`,
          'Content-Type': 'application/json',
          Accept: 'application/json',
          'Idempotency-Key': idempotency,
        },
        body: JSON.stringify({
          from: from.trim(),
          to: to.trim(),
          subject,
          html,
          text,
        }),
      });
    } catch (error) {
      if (isTimeoutError(error, signal)) {
        return refused(`Délai dépassé après ${timeoutMs} ms lors de l’appel à Resend.`);
      }
      return refused(`Erreur réseau lors de l’appel à Resend : ${describeError(error)}`);
    }

    const httpStatus = Number.isInteger(response?.status) ? response.status : null;
    const body = await readResponseBody(response);
    const payload = parseJson(body);

    if (httpStatus === null || httpStatus < 200 || httpStatus >= 300) {
      const providerName = typeof payload?.name === 'string' ? payload.name : null;
      const providerMessage = stringifyProviderMessage(payload?.message);
      const statusLabel = httpStatus === null ? 'Réponse HTTP invalide' : `HTTP ${httpStatus}`;
      const nameLabel = providerName === null ? '' : ` (${providerName})`;
      const messageLabel = providerMessage === null ? '' : ` : ${providerMessage}`;
      return refused(`${statusLabel}${nameLabel}${messageLabel}`, { httpStatus, providerName });
    }

    const providerMessageId =
      typeof payload?.id === 'string'
        ? redactSecrets(payload.id.trim(), secrets)
        : '';
    if (providerMessageId === '') {
      return refused(
        `Réponse Resend invalide (HTTP ${httpStatus}) : identifiant de message absent.`,
        { httpStatus },
      );
    }

    if (log && typeof log.info === 'function') {
      try {
        log.info('resend_email_accepted', { httpStatus, providerMessageId });
      } catch {
        // Logging must never take the delivery path down.
      }
    }
    return {
      status: 'accepted',
      httpStatus,
      providerMessageId,
      providerName: null,
      reason: null,
    };
  }

  return { send };
}
