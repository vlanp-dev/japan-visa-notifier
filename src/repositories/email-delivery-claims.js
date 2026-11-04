import {
  isoNow,
  requireIsoTimestamp,
  requireNonEmptyString,
  toPositiveInteger,
} from './validation.js';

function rowToClaim(row) {
  if (!row) {
    return null;
  }
  return {
    episodeId: row.episode_id,
    claimToken: row.claim_token,
    claimedAt: row.claimed_at,
    completedAt: row.completed_at,
    outcome: row.outcome,
    pending: row.completed_at === null,
  };
}

/**
 * Durable, cross-process claim for the single email attempt of an episode.
 *
 * The claim is inserted and committed before the provider request. A second
 * process, or a process that restarts while a claim is still open, therefore
 * cannot make another provider request. The final `accepted`, `refused` or
 * `not_attempted` attempt and the completion of the claim are written in one
 * transaction.
 */
export class EmailDeliveryClaimsRepository {
  constructor(db, { now = () => new Date(), emailAttempts = null } = {}) {
    if (!emailAttempts || typeof emailAttempts.create !== 'function') {
      throw new TypeError('emailAttempts repository is required.');
    }
    this.db = db;
    this.now = now;
    this.emailAttempts = emailAttempts;
  }

  findByEpisodeId(episodeId) {
    toPositiveInteger(episodeId, 'episodeId');
    const row = this.db
      .prepare('SELECT * FROM email_delivery_claims WHERE episode_id = ?')
      .get(episodeId);
    return rowToClaim(row);
  }

  /**
   * Insert the pre-send claim. Returns the existing claim when another writer
   * already reserved this episode, so the caller must not call the provider.
   */
  claim(episodeId, { claimToken, claimedAt } = {}) {
    toPositiveInteger(episodeId, 'episodeId');
    requireNonEmptyString(claimToken, 'claimToken');
    const at = claimedAt === undefined ? isoNow(this.now) : requireIsoTimestamp(claimedAt, 'claimedAt');

    const result = this.db
      .prepare(`
        INSERT INTO email_delivery_claims (episode_id, claim_token, claimed_at)
        VALUES (?, ?, ?)
        ON CONFLICT (episode_id) DO NOTHING
      `)
      .run(episodeId, claimToken, at);
    return {
      claimed: result.changes > 0,
      claim: this.findByEpisodeId(episodeId),
    };
  }

  /**
   * Persist the final attempt and complete the claim atomically. If an attempt
   * already exists (for example a concurrent writer won the insert), the
   * persisted attempt is returned instead of the caller's local outcome, so the
   * returned value can never disagree with the database.
   */
  complete({
    episodeId,
    claimToken,
    status,
    providerMessageId = null,
    reason = null,
    attemptedAt,
  } = {}) {
    toPositiveInteger(episodeId, 'episodeId');
    requireNonEmptyString(claimToken, 'claimToken');
    const at = attemptedAt === undefined ? isoNow(this.now) : requireIsoTimestamp(attemptedAt, 'attemptedAt');

    this.db.exec('BEGIN IMMEDIATE');
    try {
      let attempt = this.emailAttempts.findByEpisodeId(episodeId);
      let created = false;
      if (attempt === null) {
        attempt = this.emailAttempts.create({
          kind: 'episode',
          episodeId,
          status,
          providerMessageId,
          reason,
          attemptedAt: at,
        });
        created = true;
      }

      const completed = this.db
        .prepare(`
          UPDATE email_delivery_claims
          SET completed_at = ?, outcome = ?
          WHERE episode_id = ? AND claim_token = ? AND completed_at IS NULL
        `)
        .run(at, attempt.status, episodeId, claimToken);

      this.db.exec('COMMIT');
      return {
        attempt,
        created,
        completed: completed.changes > 0,
      };
    } catch (error) {
      try {
        this.db.exec('ROLLBACK');
      } catch {
        // Preserve the original error.
      }
      throw error;
    }
  }
}
