import {
  renderAckAlreadyAcknowledgedPage,
  renderAckConfirmationPage,
  renderAckErrorPage,
  renderAckSuccessPage,
} from './pages.js';

const REJECTION_PAGES = Object.freeze({
  malformed: {
    statusCode: 400,
    title: 'Lien d’acquittement invalide',
    message: 'Ce lien d’acquittement est incomplet ou mal formé.',
  },
  invalid_signature: {
    statusCode: 400,
    title: 'Lien d’acquittement invalide',
    message: 'La signature de ce lien d’acquittement est invalide.',
  },
  mismatch: {
    statusCode: 400,
    title: 'Lien d’acquittement invalide',
    message: 'Ce lien d’acquittement ne correspond pas à l’épisode enregistré.',
  },
  expired: {
    statusCode: 410,
    title: 'Lien d’acquittement expiré',
    message: 'Ce lien d’acquittement a expiré. Ouvrez le service pour acquitter l’épisode.',
  },
  not_found: {
    statusCode: 404,
    title: 'Épisode introuvable',
    message: 'Aucun épisode ne correspond à ce lien d’acquittement.',
  },
});

function sendRejection(ctx, result, { logEvent }) {
  if (result.reason === 'already_acknowledged') {
    if (logEvent) {
      ctx.logger.info(logEvent, {
        outcome: 'already_acknowledged',
        episodeId: result.episode?.id ?? null,
      });
    }
    ctx.sendHtml(
      409,
      renderAckAlreadyAcknowledgedPage({ episode: result.episode }),
    );
    return;
  }
  const page = REJECTION_PAGES[result.reason] ?? REJECTION_PAGES.malformed;
  if (logEvent) {
    ctx.logger.warn(logEvent, {
      outcome: result.reason,
      episodeId: result.episode?.id ?? null,
    });
  }
  ctx.sendHtml(
    page.statusCode,
    renderAckErrorPage({ title: page.title, message: page.message }),
  );
}

function missingToken(ctx, { logEvent }) {
  if (logEvent) {
    ctx.logger.warn(logEvent, { outcome: 'missing_token', episodeId: null });
  }
  ctx.sendHtml(
    400,
    renderAckErrorPage({
      title: 'Lien d’acquittement invalide',
      message: 'Ce lien d’acquittement ne contient aucun jeton.',
    }),
  );
}

/**
 * Public, signed acknowledgement links. The token itself is the capability:
 * these routes opt out of Basic auth and CSRF, but a state change requires a
 * valid HMAC signature, matching nonce and an unexpired token.
 */
export function registerAckRoutes(app, { episodeService } = {}) {
  if (!episodeService || typeof episodeService.inspectAcknowledgement !== 'function') {
    throw new TypeError('registerAckRoutes requires an episodeService.');
  }

  app.get(
    '/ack',
    (ctx) => missingToken(ctx, { logEvent: 'acknowledgement_rejected' }),
    { public: true },
  );
  app.post(
    '/ack',
    (ctx) => missingToken(ctx, { logEvent: 'acknowledgement_rejected' }),
    { public: true, csrf: false },
  );

  app.get(
    '/ack/:token',
    (ctx) => {
      const result = episodeService.inspectAcknowledgement(ctx.params.token);
      if (!result.ok) {
        sendRejection(ctx, result, { logEvent: 'acknowledgement_rejected' });
        return;
      }
      ctx.sendHtml(200, renderAckConfirmationPage({ episode: result.episode }));
    },
    { public: true },
  );

  app.post(
    '/ack/:token',
    (ctx) => {
      const result = episodeService.acknowledge(ctx.params.token);
      if (!result.ok) {
        sendRejection(ctx, result, { logEvent: 'acknowledgement_rejected' });
        return;
      }
      ctx.logger.info('episode_acknowledged', { episodeId: result.episode.id });
      ctx.sendHtml(200, renderAckSuccessPage({ episode: result.episode }));
    },
    { public: true, csrf: false },
  );
}
