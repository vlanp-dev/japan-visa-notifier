import { randomUUID } from 'node:crypto';
import { checkBasicAuth } from './basic-auth.js';
import { parseBody, readBody } from './body.js';
import { parseCookies } from './cookies.js';
import { csrfTokensMatch, ensureCsrfCookie, submittedCsrfToken } from './csrf.js';
import { HttpError } from './errors.js';
import { applySecurityHeaders } from './security.js';

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);
const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

function escapeRegex(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function compilePattern(pattern) {
  if (typeof pattern !== 'string' || !pattern.startsWith('/')) {
    throw new TypeError('Route pattern must start with "/".');
  }
  const normalized = pattern === '/' ? '/' : pattern.replace(/\/+$/, '');
  if (normalized === '/') {
    return { regex: /^\/$/, keys: [] };
  }
  const keys = [];
  const source = normalized
    .slice(1)
    .split('/')
    .map((segment) => {
      if (segment.startsWith(':') && segment.length > 1) {
        keys.push(segment.slice(1));
        return '([^/]+)';
      }
      return escapeRegex(segment);
    })
    .join('/');
  return { regex: new RegExp(`^/${source}/?$`), keys };
}

function decodeParam(value) {
  try {
    return decodeURIComponent(value);
  } catch {
    throw new HttpError(400, 'Requête invalide.');
  }
}

export function sendJson(res, statusCode, payload, headers = {}) {
  const body = JSON.stringify(payload);
  res.statusCode = statusCode;
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.setHeader('Content-Length', Buffer.byteLength(body));
  for (const [name, value] of Object.entries(headers)) {
    res.setHeader(name, value);
  }
  res.end(res.req?.method === 'HEAD' ? undefined : body);
}

export function sendText(res, statusCode, text, headers = {}) {
  res.statusCode = statusCode;
  res.setHeader('Content-Type', 'text/plain; charset=utf-8');
  res.setHeader('Content-Length', Buffer.byteLength(text));
  for (const [name, value] of Object.entries(headers)) {
    res.setHeader(name, value);
  }
  res.end(res.req?.method === 'HEAD' ? undefined : text);
}

export function sendHtml(res, statusCode, html, headers = {}) {
  res.statusCode = statusCode;
  res.setHeader('Content-Type', 'text/html; charset=utf-8');
  res.setHeader('Content-Length', Buffer.byteLength(html));
  for (const [name, value] of Object.entries(headers)) {
    res.setHeader(name, value);
  }
  res.end(res.req?.method === 'HEAD' ? undefined : html);
}

export function redirect(res, location, statusCode = 303) {
  if (typeof location !== 'string' || location === '' || /[\r\n]/.test(location)) {
    throw new TypeError('Invalid redirect location.');
  }
  res.statusCode = statusCode;
  res.setHeader('Location', location);
  res.end();
}

function attachHelpers(context) {
  context.sendJson = (statusCode, payload, headers) => sendJson(context.res, statusCode, payload, headers);
  context.sendText = (statusCode, text, headers) => sendText(context.res, statusCode, text, headers);
  context.sendHtml = (statusCode, html, headers) => sendHtml(context.res, statusCode, html, headers);
  context.redirect = (location, statusCode) => redirect(context.res, location, statusCode);
  return context;
}

function handleError(error, { req, res, logger, route, method }) {
  const statusCode = error instanceof HttpError ? error.statusCode : 500;
  if (statusCode >= 500) {
    logger.error('http_request_failed', { error, method, route });
  } else if (statusCode === 413) {
    logger.warn('http_request_rejected', { statusCode, method, route });
  }
  if (res.headersSent) {
    res.destroy();
    return;
  }
  if (statusCode === 401) {
    res.setHeader('WWW-Authenticate', 'Basic realm="Alertes ambassade", charset="UTF-8"');
  }
  if (statusCode === 413) {
    res.setHeader('Connection', 'close');
  }
  const message = statusCode >= 500 ? 'Erreur interne du serveur.' : error.message;
  sendText(res, statusCode, message);
  if (statusCode === 413) {
    res.once('finish', () => req.destroy());
  }
}

/**
 * Build the HTTP application. `registerRoutes(app)` is the extension point used
 * by later tasks and by tests; routes added there inherit Basic auth and CSRF
 * unless they opt out with `{ public: true }` / `{ csrf: false }`.
 */
export function createApp({ config, logger, db, repositories, registerRoutes } = {}) {
  if (!config || !logger || !repositories) {
    throw new TypeError('createApp requires config, logger and repositories.');
  }

  const routes = [];

  function add(method, pattern, handler, options = {}) {
    if (typeof handler !== 'function') {
      throw new TypeError('Route handler must be a function.');
    }
    if (!SAFE_METHODS.has(method) && !UNSAFE_METHODS.has(method)) {
      throw new TypeError(`Unsupported HTTP method: ${method}`);
    }
    const { regex, keys } = compilePattern(pattern);
    routes.push({ method, pattern, regex, keys, handler, options });
    return app;
  }

  const app = {
    routes,
    add,
    get: (pattern, handler, options) => add('GET', pattern, handler, options),
    post: (pattern, handler, options) => add('POST', pattern, handler, options),
    put: (pattern, handler, options) => add('PUT', pattern, handler, options),
    patch: (pattern, handler, options) => add('PATCH', pattern, handler, options),
    delete: (pattern, handler, options) => add('DELETE', pattern, handler, options),
    async handle(req, res) {
      applySecurityHeaders(res);

      const method = (req.method ?? 'GET').toUpperCase();
      let url;
      try {
        url = new URL(req.url ?? '/', 'http://localhost');
      } catch {
        sendText(res, 400, 'Requête invalide.');
        return;
      }

      const match = matchRoute(routes, method, url.pathname);
      const cookies = parseCookies(req.headers.cookie);
      const context = {
        req,
        res,
        url,
        method,
        params: match?.params ?? {},
        route: match?.route.pattern ?? null,
        cookies,
        csrfToken: cookies[config.csrfCookieName] ?? null,
        config,
        logger,
        repositories,
        requestId: randomUUID(),
      };

      try {
        if (match?.route.options.public !== true) {
          if (!checkBasicAuth(req.headers.authorization, config.operator)) {
            res.setHeader('WWW-Authenticate', 'Basic realm="Alertes ambassade", charset="UTF-8"');
            sendText(res, 401, 'Authentification requise.');
            return;
          }
        }

        if (SAFE_METHODS.has(method)) {
          const token = ensureCsrfCookie(req, res, config, cookies);
          cookies[config.csrfCookieName] = token;
          context.csrfToken = token;
        } else if (UNSAFE_METHODS.has(method)) {
          const body = await readBody(req, config.maxBodyBytes);
          Object.assign(context, parseBody(body, req.headers['content-type']));
          if (match && match.route.options.csrf !== false) {
            const submitted = submittedCsrfToken(req, context.form, config.csrfHeaderName);
            if (!csrfTokensMatch(cookies[config.csrfCookieName], submitted)) {
              sendText(res, 403, 'Jeton CSRF invalide.');
              return;
            }
          }
        }

        if (!match) {
          sendText(res, 404, 'Ressource introuvable.');
          return;
        }

        attachHelpers(context);
        await match.route.handler(context);
        if (!res.writableEnded) {
          res.statusCode = 204;
          res.end();
        }
      } catch (error) {
        handleError(error, {
          req,
          res,
          logger,
          route: match?.route.pattern ?? null,
          method,
        });
      }
    },
  };

  add(
    'GET',
    '/health',
    ({ res }) => {
      let database = 'ok';
      try {
        db.prepare('SELECT 1 AS ok').get();
      } catch {
        database = 'unavailable';
      }
      sendJson(res, database === 'ok' ? 200 : 503, {
        status: database === 'ok' ? 'ok' : 'degraded',
        database,
      });
    },
    { public: true },
  );

  if (typeof registerRoutes === 'function') {
    registerRoutes(app);
  }

  return app;
}

function matchRoute(routes, method, pathname) {
  for (const route of routes) {
    if (route.method !== method && !(method === 'HEAD' && route.method === 'GET')) {
      continue;
    }
    const match = route.regex.exec(pathname);
    if (!match) {
      continue;
    }
    const params = {};
    route.keys.forEach((key, index) => {
      params[key] = decodeParam(match[index + 1]);
    });
    return { route, params };
  }
  return null;
}
