import { createServer } from 'node:http';
import { createAckTokenSigner } from './ack/ack-token-signer.js';
import { registerAckRoutes } from './ack/routes.js';
import { loadConfig } from './config.js';
import { openDatabase } from './db.js';
import { EmailService } from './email/email-service.js';
import { createResendClient } from './email/resend-client.js';
import { EmailSettingsService } from './email/settings.js';
import { createApp } from './http/app.js';
import { createLogger } from './logger.js';
import { createRepositories } from './repositories/index.js';
import { EpisodeService } from './services/episode-service.js';
import { registerOperatorUiRoutes } from './ui/routes.js';

/**
 * Wire configuration, logging, SQLite and the HTTP application into a service
 * object. Starting the service requires no email configuration whatsoever, and
 * no provider request is made until an episode is delivered or a test email is
 * requested.
 */
export function createService({
  env = process.env,
  logger,
  ackTokenSigner,
  episodeService,
  emailService,
  resendClient,
} = {}) {
  const config = loadConfig(env);
  const db = openDatabase(config.databasePath);
  const repositories = createRepositories(db);
  const emailSettings = new EmailSettingsService({
    repositories,
    apiKeyPath: config.resendApiKeyPath,
  });
  const tokenSigner =
    ackTokenSigner ??
    createAckTokenSigner({
      appSecrets: repositories.appSecrets,
      secret: config.ackTokenSecret,
      publicBaseUrl: config.publicBaseUrl,
    });
  const episodes =
    episodeService ??
    new EpisodeService({
      repositories,
      tokenSigner,
    });
  const log =
    logger ??
    createLogger({
      level: config.logLevel,
      secrets: [config.operator.password, tokenSigner.secret],
    });
  const mailer =
    emailService ??
    new EmailService({
      repositories,
      emailSettings,
      resendClient: resendClient ?? createResendClient({ logger: log }),
      logger: log,
    });
  const app = createApp({
    config,
    logger: log,
    db,
    repositories,
    registerRoutes: (httpApp) => {
      registerAckRoutes(httpApp, { episodeService: episodes });
      registerOperatorUiRoutes(httpApp, {
        repositories,
        episodeService: episodes,
        emailService: mailer,
        emailSettings,
        tokenSigner,
      });
    },
  });
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
    ackTokenSigner: tokenSigner,
    episodeService: episodes,
    emailSettings,
    emailService: mailer,
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
