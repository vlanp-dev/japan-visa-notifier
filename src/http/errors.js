export class HttpError extends Error {
  constructor(statusCode, message, { expose = true } = {}) {
    super(message);
    this.name = 'HttpError';
    this.statusCode = statusCode;
    this.expose = expose;
  }
}

export const HTTP_ERRORS = Object.freeze({
  badRequest: () => new HttpError(400, 'Requête invalide.'),
  unauthorized: () => new HttpError(401, 'Authentification requise.'),
  forbidden: () => new HttpError(403, 'Requête refusée.'),
  notFound: () => new HttpError(404, 'Ressource introuvable.'),
  payloadTooLarge: () => new HttpError(413, 'Corps de requête trop volumineux.'),
});
