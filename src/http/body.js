import { HttpError } from './errors.js';

const EMPTY = Buffer.alloc(0);

/**
 * Read a request body up to `limit` bytes. Content-Length is rejected before
 * reading, and streaming is aborted as soon as the limit is exceeded.
 */
export function readBody(req, limit) {
  if (!Number.isInteger(limit) || limit < 1) {
    throw new TypeError('limit must be a positive integer.');
  }

  const declared = req.headers['content-length'];
  if (declared !== undefined) {
    if (typeof declared !== 'string' || !/^\d+$/.test(declared)) {
      return Promise.reject(new HttpError(400, 'Requête invalide.'));
    }
    if (Number(declared) > limit) {
      req.resume();
      return Promise.reject(new HttpError(413, 'Corps de requête trop volumineux.'));
    }
  }

  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    let settled = false;

    function cleanup() {
      req.off('data', onData);
      req.off('end', onEnd);
      req.off('error', onError);
      req.off('aborted', onAborted);
    }

    function fail(error) {
      if (settled) {
        return;
      }
      settled = true;
      cleanup();
      req.resume();
      reject(error);
    }

    function onData(chunk) {
      if (settled) {
        return;
      }
      size += chunk.length;
      if (size > limit) {
        fail(new HttpError(413, 'Corps de requête trop volumineux.'));
        return;
      }
      chunks.push(chunk);
    }

    function onEnd() {
      if (settled) {
        return;
      }
      settled = true;
      cleanup();
      resolve(chunks.length === 0 ? EMPTY : Buffer.concat(chunks, size));
    }

    function onError() {
      fail(new HttpError(400, 'Requête invalide.'));
    }

    function onAborted() {
      fail(new HttpError(400, 'Requête interrompue.'));
    }

    req.on('data', onData);
    req.on('end', onEnd);
    req.on('error', onError);
    req.on('aborted', onAborted);
  });
}

/**
 * Parse an already-read body according to Content-Type. Returns an object with
 * `body` plus one of `form`, `json` or `text`.
 */
export function parseBody(buffer, contentTypeHeader) {
  const mediaType = String(contentTypeHeader ?? '').split(';')[0].trim().toLowerCase();
  if (buffer.length === 0) {
    return { body: buffer };
  }
  if (mediaType === 'application/x-www-form-urlencoded') {
    const params = new URLSearchParams(buffer.toString('utf8'));
    return { body: buffer, form: Object.fromEntries(params), formParams: params };
  }
  if (mediaType === 'application/json') {
    let json;
    try {
      json = JSON.parse(buffer.toString('utf8'));
    } catch {
      throw new HttpError(400, 'Corps JSON invalide.');
    }
    return { body: buffer, json };
  }
  return { body: buffer, text: buffer.toString('utf8') };
}
