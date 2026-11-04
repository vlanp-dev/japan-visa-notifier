import { createServer } from 'node:http';
import { loadConfig } from './config.js';
import { openDatabase } from './db.js';
import { createApp } from './http/app.js';
import { createLogger } from './logger.js';
import { createRepositories } from './repositories/index.js';

/**
 * Wire configuration, logging, SQLite and the HTTP application into a service
 * object. Starting the service requires no email configuration whatsoever.
 */
export function createService({ env = process.env, logger } = {}) {
  const config = loadConfig(env);
  const log =
    logger ??
    createLogger({
      level: config.logLevel,
      secrets: [config.operator.password],
    });
  const db = openDatabase(config.databasePath);
  const repositories = createRepositories(db);
  const app = createApp({ config, logger: log, db, repositories });
  const server = createServer((req, res) => {
    app.handle(req, res).catch((error) => {
      log.error('http_unhandled_failure', { error });
      if (!res.headersSent) {
        res.statusCode = 500;
        res.setHeader('Content-Type', 'text/plain; charset=utf-8');
        res.end('Erreur interne du serveur.');
      } else {
        res.destroy();
      }
    });
  });

  let closed = false;

  return {
    config,
    logger: log,
    db,
    repositories,
    app,
    server,
    listen() {
      return new Promise((resolve, reject) => {
        const onError = (error) => {
          server.off('listening', onListening);
          reject(error);
        };
        const onListening = () => {
          server.off('error', onError);
          resolve(server.address());
        };
        server.once('error', onError);
        server.once('listening', onListening);
        server.listen(config.port, config.host);
      });
    },
    async close() {
      if (closed) {
        return;
      }
      closed = true;
      await new Promise((resolve) => {
        if (!server.listening) {
          resolve();
          return;
        }
        server.close(() => resolve());
        server.closeIdleConnections?.();
      });
      db.close();
    },
  };
}
