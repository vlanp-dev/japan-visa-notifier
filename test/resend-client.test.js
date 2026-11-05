import assert from 'node:assert/strict';
import test from 'node:test';
import {
  MAX_REASON_LENGTH,
  REDACTED_SECRET,
  RESEND_EMAILS_ENDPOINT,
  createResendClient,
} from '../src/email/resend-client.js';
import { createLogger } from '../src/logger.js';

const API_KEY = 're_test_1234567890_secret_key';
const MESSAGE = {
  from: 'Alertes ambassade <alertes@mail.vlanp.com>',
  to: 'operateur@example.com',
  subject: 'Alerte ambassade',
  html: '<p>Le contenu a changé.</p>',
  text: 'Le contenu a changé.',
  idempotencyKey: 'episode-42',
};

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function recordingFetch(response) {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({ url, options });
    return response;
  };
  return { calls, fetchImpl };
}

function captureLogger() {
  const lines = [];
  const stream = {
    write(chunk) {
      lines.push(String(chunk));
      return true;
    },
  };
  return { lines, logger: createLogger({ level: 'debug', stream }) };
}

test('a 200 response is accepted with the provider message id and a single recipient', async () => {
  const { calls, fetchImpl } = recordingFetch(
    jsonResponse({ id: '49a3999c-0ce1-4ea6-ab68-afcd6dc2e794' }),
  );
  const { lines, logger } = captureLogger();
  const client = createResendClient({ apiKey: API_KEY, fetchImpl, logger });

  const result = await client.send(MESSAGE);

  assert.deepEqual(result, {
    status: 'accepted',
    httpStatus: 200,
    providerMessageId: '49a3999c-0ce1-4ea6-ab68-afcd6dc2e794',
    providerName: null,
    reason: null,
  });
  assert.equal(calls.length, 1);
  const [{ url, options }] = calls;
  assert.equal(url, RESEND_EMAILS_ENDPOINT);
  assert.equal(options.method, 'POST');
  assert.equal(options.redirect, 'error');
  assert.ok(options.signal instanceof AbortSignal);
  assert.equal(options.headers.Authorization, `Bearer ${API_KEY}`);
  assert.equal(options.headers['Content-Type'], 'application/json');
  assert.equal(options.headers['Idempotency-Key'], 'episode-42');
  assert.deepEqual(JSON.parse(options.body), {
    from: MESSAGE.from,
    to: MESSAGE.to,
    subject: MESSAGE.subject,
    html: MESSAGE.html,
    text: MESSAGE.text,
  });
  assert.equal(typeof JSON.parse(options.body).to, 'string');
  const output = lines.join('');
  assert.ok(output.includes('resend_email_accepted'));
  assert.ok(!output.includes(API_KEY));
  assert.ok(!JSON.stringify(result).includes(API_KEY));
});

test('an invalid API key is refused with the provider message, name and status', async () => {
  const { fetchImpl } = recordingFetch(
    jsonResponse(
      {
        statusCode: 401,
        name: 'validation_error',
        message: 'API key is invalid',
      },
      401,
    ),
  );
  const client = createResendClient({ apiKey: API_KEY, fetchImpl });

  const result = await client.send(MESSAGE);

  assert.equal(result.status, 'refused');
  assert.equal(result.httpStatus, 401);
  assert.equal(result.providerName, 'validation_error');
  assert.equal(result.providerMessageId, null);
  assert.match(result.reason, /HTTP 401/);
  assert.match(result.reason, /validation_error/);
  assert.match(result.reason, /API key is invalid/);
});

test('an unverified sending domain is refused with the provider reason', async () => {
  const { fetchImpl } = recordingFetch(
    jsonResponse(
      {
        statusCode: 403,
        name: 'validation_error',
        message: 'The vlanp.com domain is not verified. Please, add and verify your domain.',
      },
      403,
    ),
  );
  const client = createResendClient({ apiKey: API_KEY, fetchImpl });

  const result = await client.send(MESSAGE);

  assert.equal(result.status, 'refused');
  assert.equal(result.httpStatus, 403);
  assert.equal(result.providerName, 'validation_error');
  assert.match(result.reason, /HTTP 403/);
  assert.match(result.reason, /domain is not verified/);
});

test('a validation error is refused with the provider detail', async () => {
  const { fetchImpl } = recordingFetch(
    jsonResponse(
      {
        statusCode: 422,
        name: 'validation_error',
        message: 'The `html` field is required.',
      },
      422,
    ),
  );
  const client = createResendClient({ apiKey: API_KEY, fetchImpl });

  const result = await client.send(MESSAGE);

  assert.equal(result.status, 'refused');
  assert.equal(result.httpStatus, 422);
  assert.match(result.reason, /HTTP 422/);
  assert.match(result.reason, /`html` field is required/);
});

test('a timeout is refused without throwing', async () => {
  const client = createResendClient({
    apiKey: API_KEY,
    timeoutMs: 20,
    fetchImpl: (_url, { signal }) =>
      new Promise((_resolve, reject) => {
        const fail = () => {
          const error = new Error('The operation was aborted due to timeout');
          error.name = 'TimeoutError';
          reject(error);
        };
        if (signal.aborted) {
          fail();
        } else {
          signal.addEventListener('abort', fail, { once: true });
        }
      }),
  });

  const result = await client.send(MESSAGE);

  assert.equal(result.status, 'refused');
  assert.equal(result.httpStatus, null);
  assert.match(result.reason, /Délai dépassé après 20 ms/);
});

test('a network failure is refused without throwing', async () => {
  const client = createResendClient({
    apiKey: API_KEY,
    fetchImpl: async () => {
      throw new TypeError('fetch failed', {
        cause: new Error('getaddrinfo ENOTFOUND api.resend.com'),
      });
    },
  });

  const result = await client.send(MESSAGE);

  assert.equal(result.status, 'refused');
  assert.equal(result.httpStatus, null);
  assert.match(result.reason, /Erreur réseau/);
  assert.match(result.reason, /fetch failed/);
  assert.match(result.reason, /ENOTFOUND/);
});

test('the API key is redacted from results and logs', async () => {
  const { lines, logger } = captureLogger();
  const client = createResendClient({
    apiKey: API_KEY,
    logger,
    fetchImpl: async () =>
      jsonResponse(
        {
          statusCode: 401,
          name: 'validation_error',
          message: `API key ${API_KEY} is invalid\nPlease create a new key.`,
        },
        401,
      ),
  });

  const result = await client.send(MESSAGE);

  assert.equal(result.status, 'refused');
  assert.ok(!JSON.stringify(result).includes(API_KEY));
  assert.ok(result.reason.includes(REDACTED_SECRET));
  assert.ok(!result.reason.includes('\n'));
  assert.match(result.reason, /Please create a new key/);
  const output = lines.join('');
  assert.ok(output.includes('resend_email_refused'));
  assert.ok(!output.includes(API_KEY));
});

test('a per-call API key is also redacted', async () => {
  const client = createResendClient({
    fetchImpl: async () =>
      jsonResponse(
        {
          statusCode: 401,
          name: 'validation_error',
          message: `Invalid key ${API_KEY}`,
        },
        401,
      ),
  });

  const result = await client.send({ ...MESSAGE, apiKey: API_KEY });

  assert.equal(result.status, 'refused');
  assert.ok(!JSON.stringify(result).includes(API_KEY));
  assert.ok(result.reason.includes(REDACTED_SECRET));
});

test('the reason is single-line and length-limited', async () => {
  const longMessage = `First line\nSecond line ${'x'.repeat(2_000)}`;
  const client = createResendClient({
    apiKey: API_KEY,
    fetchImpl: async () =>
      jsonResponse({ statusCode: 500, name: 'application_error', message: longMessage }, 500),
  });

  const result = await client.send(MESSAGE);

  assert.equal(result.status, 'refused');
  assert.ok(result.reason.length <= MAX_REASON_LENGTH);
  assert.ok(result.reason.endsWith('...'));
  assert.ok(!result.reason.includes('\n'));
});

test('a redirect status is refused and redirects are not followed', async () => {
  const { calls, fetchImpl } = recordingFetch(
    new Response('Moved Permanently', { status: 301, headers: { location: 'https://example.com' } }),
  );
  const client = createResendClient({ apiKey: API_KEY, fetchImpl });

  const result = await client.send(MESSAGE);

  assert.equal(calls[0].options.redirect, 'error');
  assert.equal(result.status, 'refused');
  assert.equal(result.httpStatus, 301);
  assert.match(result.reason, /HTTP 301/);
});

test('a non-JSON provider error still preserves the HTTP status', async () => {
  const { fetchImpl } = recordingFetch(
    new Response('Internal Server Error', { status: 500 }),
  );
  const client = createResendClient({ apiKey: API_KEY, fetchImpl });

  const result = await client.send(MESSAGE);

  assert.equal(result.status, 'refused');
  assert.equal(result.httpStatus, 500);
  assert.match(result.reason, /HTTP 500/);
});

test('a 2xx response without a message id is not recorded as accepted', async () => {
  const { fetchImpl } = recordingFetch(jsonResponse({ ok: true }));
  const client = createResendClient({ apiKey: API_KEY, fetchImpl });

  const result = await client.send(MESSAGE);

  assert.equal(result.status, 'refused');
  assert.equal(result.httpStatus, 200);
  assert.match(result.reason, /identifiant de message absent/);
});

test('a missing API key is refused without calling the provider', async () => {
  const { calls, fetchImpl } = recordingFetch(jsonResponse({ id: 'unused' }));
  const client = createResendClient({ fetchImpl });

  const result = await client.send(MESSAGE);

  assert.equal(calls.length, 0);
  assert.equal(result.status, 'refused');
  assert.match(result.reason, /Configuration manquante/);
  assert.match(result.reason, /clé API Resend absente/);
});

test('a recipient list is refused without calling the provider', async () => {
  const { calls, fetchImpl } = recordingFetch(jsonResponse({ id: 'unused' }));
  const client = createResendClient({ apiKey: API_KEY, fetchImpl });

  const result = await client.send({ ...MESSAGE, to: ['one@example.com', 'two@example.com'] });

  assert.equal(calls.length, 0);
  assert.equal(result.status, 'refused');
  assert.match(result.reason, /destinataire/);
});

test('an omitted idempotency key is generated and sent', async () => {
  const { calls, fetchImpl } = recordingFetch(jsonResponse({ id: 'generated-key' }));
  const client = createResendClient({ apiKey: API_KEY, fetchImpl });

  const { idempotencyKey, ...withoutKey } = MESSAGE;
  const result = await client.send(withoutKey);

  assert.equal(result.status, 'accepted');
  const header = calls[0].options.headers['Idempotency-Key'];
  assert.equal(typeof header, 'string');
  assert.ok(header.length > 0);
  assert.ok(header.length <= 256);
});
