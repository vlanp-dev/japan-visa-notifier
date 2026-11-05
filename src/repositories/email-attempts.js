import {
  isoNow,
  requireIsoTimestamp,
  requireNonEmptyString,
  requireOptionalString,
  toPositiveInteger,
} from './validation.js';

export const EMAIL_ATTEMPT_STATUSES = Object.freeze(['accepted', 'refused', 'not_attempted']);
export const EMAIL_ATTEMPT_KINDS = Object.freeze(['episode', 'test']);

function rowToAttempt(row) {
  if (!row) {
    return null;
  }
  return {
    id: row.id,
    kind: row.kind,
    episodeId: row.episode_id,
    status: row.status,
    providerMessageId: row.provider_message_id,
    reason: row.reason,
    attemptedAt: row.attempted_at,
  };
}

export class EmailAttemptsRepository {
  constructor(db, { now = () => new Date() } = {}) {
    this.db = db;
    this.now = now;
  }

  create(
    {
      kind = 'episode',
      episodeId = null,
      status,
      providerMessageId = null,
      reason = null,
      attemptedAt,
    } = {},
  ) {
    if (!EMAIL_ATTEMPT_KINDS.includes(kind)) {
      throw new TypeError(`kind must be one of: ${EMAIL_ATTEMPT_KINDS.join(', ')}.`);
    }
    if (!EMAIL_ATTEMPT_STATUSES.includes(status)) {
      throw new TypeError(`status must be one of: ${EMAIL_ATTEMPT_STATUSES.join(', ')}.`);
    }
    if (kind === 'episode') {
      toPositiveInteger(episodeId, 'episodeId');
    } else if (episodeId !== null && episodeId !== undefined) {
      throw new TypeError('test attempts must not reference an episode.');
    }
    requireOptionalString(providerMessageId, 'providerMessageId');
    requireOptionalString(reason, 'reason');
    if ((status === 'refused' || status === 'not_attempted') && (reason === null || reason.trim() === '')) {
      throw new TypeError(`A ${status} attempt requires a reason.`);
    }
    if (status === 'accepted' && reason !== null) {
      throw new TypeError('An accepted attempt must not carry a failure reason.');
    }
    const at = attemptedAt === undefined ? isoNow(this.now) : requireIsoTimestamp(attemptedAt, 'attemptedAt');

    const result = this.db
      .prepare(`
        INSERT INTO email_attempts
          (kind, episode_id, status, provider_message_id, reason, attempted_at)
        VALUES (?, ?, ?, ?, ?, ?)
      `)
      .run(kind, kind === 'episode' ? episodeId : null, status, providerMessageId, reason, at);
    return this.findById(Number(result.lastInsertRowid));
  }

  findById(id) {
    toPositiveInteger(id, 'id');
    const row = this.db.prepare('SELECT * FROM email_attempts WHERE id = ?').get(id);
    return rowToAttempt(row);
  }

  findByEpisodeId(episodeId) {
    toPositiveInteger(episodeId, 'episodeId');
    const row = this.db
      .prepare(`
        SELECT * FROM email_attempts
        WHERE kind = 'episode' AND episode_id = ?
      `)
      .get(episodeId);
    return rowToAttempt(row);
  }

  list({ limit = 50, offset = 0 } = {}) {
    if (!Number.isInteger(limit) || limit < 1 || limit > 500) {
      throw new TypeError('limit must be an integer between 1 and 500.');
    }
    if (!Number.isInteger(offset) || offset < 0) {
      throw new TypeError('offset must be a non-negative integer.');
    }
    return this.db
      .prepare(`
        SELECT * FROM email_attempts
        ORDER BY id DESC
        LIMIT ? OFFSET ?
      `)
      .all(limit, offset)
      .map(rowToAttempt);
  }

  /**
   * Number of refused sends recorded after the most recent accepted send.
   * `not_attempted` rows are intentionally ignored. Optionally restrict to one
   * kind (`episode` or `test`).
   */
  countRefusedSinceLastAccepted({ kind = null } = {}) {
    if (kind !== null && !EMAIL_ATTEMPT_KINDS.includes(kind)) {
      throw new TypeError(`kind must be one of: ${EMAIL_ATTEMPT_KINDS.join(', ')}.`);
    }
    const row =
      kind === null
        ? this.db
            .prepare(`
              SELECT COUNT(*) AS count FROM email_attempts
              WHERE status = 'refused'
                AND id > COALESCE(
                  (SELECT MAX(id) FROM email_attempts WHERE status = 'accepted'),
                  0
                )
            `)
            .get()
        : this.db
            .prepare(`
              SELECT COUNT(*) AS count FROM email_attempts
              WHERE kind = ? AND status = 'refused'
                AND id > COALESCE(
                  (SELECT MAX(id) FROM email_attempts WHERE kind = ? AND status = 'accepted'),
                  0
                )
            `)
            .get(kind, kind);
    return Number(row.count);
  }
}
