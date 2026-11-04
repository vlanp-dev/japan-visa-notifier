import {
  isoNow,
  requireIsoTimestamp,
  requireNonEmptyString,
  requireOptionalString,
  toPositiveInteger,
} from './validation.js';

function normalizeRule(rule, index) {
  if (rule === null || typeof rule !== 'object' || Array.isArray(rule)) {
    throw new TypeError(`rules[${index}] must be an object.`);
  }
  const ruleKey = requireNonEmptyString(rule.ruleKey, `rules[${index}].ruleKey`);
  const label = requireNonEmptyString(rule.label, `rules[${index}].label`);
  const detail = requireOptionalString(rule.detail, `rules[${index}].detail`);
  return { ruleKey, label, detail };
}

function rowToEpisode(row) {
  if (!row) {
    return null;
  }
  return {
    id: row.id,
    openedAt: row.opened_at,
    status: row.status,
    acknowledgedAt: row.acknowledged_at,
    embassyName: row.embassy_name,
    embassyPageUrl: row.embassy_page_url,
    excerpt: row.excerpt,
    contentHash: row.content_hash,
  };
}

function rowToRule(row) {
  return {
    id: row.id,
    episodeId: row.episode_id,
    ruleKey: row.rule_key,
    label: row.label,
    detail: row.detail,
    createdAt: row.created_at,
  };
}

export class EpisodesRepository {
  constructor(db, { now = () => new Date() } = {}) {
    this.db = db;
    this.now = now;
  }

  /**
   * Persist one episode and all triggered rules atomically. A duplicate rule
   * key rolls the whole episode back.
   */
  createWithRules(
    {
      openedAt,
      embassyName = null,
      embassyPageUrl = null,
      excerpt = '',
      contentHash = null,
      rules = [],
    } = {},
  ) {
    const at = openedAt === undefined ? isoNow(this.now) : requireIsoTimestamp(openedAt, 'openedAt');
    requireOptionalString(embassyName, 'embassyName');
    requireOptionalString(embassyPageUrl, 'embassyPageUrl');
    if (typeof excerpt !== 'string') {
      throw new TypeError('excerpt must be a string.');
    }
    requireOptionalString(contentHash, 'contentHash');
    if (!Array.isArray(rules) || rules.length === 0) {
      throw new TypeError('rules must be a non-empty array.');
    }
    const normalizedRules = rules.map(normalizeRule);
    const seen = new Set();
    for (const rule of normalizedRules) {
      if (seen.has(rule.ruleKey)) {
        throw new TypeError(`Duplicate rule key: ${rule.ruleKey}`);
      }
      seen.add(rule.ruleKey);
    }

    this.db.exec('BEGIN IMMEDIATE');
    try {
      const result = this.db
        .prepare(`
          INSERT INTO episodes (opened_at, status, embassy_name, embassy_page_url, excerpt, content_hash)
          VALUES (?, 'open', ?, ?, ?, ?)
        `)
        .run(at, embassyName, embassyPageUrl, excerpt, contentHash);
      const episodeId = Number(result.lastInsertRowid);
      const insertRule = this.db.prepare(`
        INSERT INTO episode_rules (episode_id, rule_key, label, detail, created_at)
        VALUES (?, ?, ?, ?, ?)
      `);
      for (const rule of normalizedRules) {
        insertRule.run(episodeId, rule.ruleKey, rule.label, rule.detail, at);
      }
      this.db.exec('COMMIT');
      return this.getById(episodeId);
    } catch (error) {
      try {
        this.db.exec('ROLLBACK');
      } catch {
        // Preserve the original error.
      }
      throw error;
    }
  }

  getById(id) {
    toPositiveInteger(id, 'id');
    const row = this.db.prepare('SELECT * FROM episodes WHERE id = ?').get(id);
    return rowToEpisode(row);
  }

  getWithRules(id) {
    const episode = this.getById(id);
    if (episode === null) {
      return null;
    }
    return { ...episode, rules: this.getRules(id) };
  }

  getRules(episodeId) {
    toPositiveInteger(episodeId, 'episodeId');
    return this.db
      .prepare(`
        SELECT * FROM episode_rules
        WHERE episode_id = ?
        ORDER BY id ASC
      `)
      .all(episodeId)
      .map(rowToRule);
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
        SELECT * FROM episodes
        ORDER BY id DESC
        LIMIT ? OFFSET ?
      `)
      .all(limit, offset)
      .map(rowToEpisode);
  }

  /** Transition an open episode to acknowledged. Returns true on transition. */
  acknowledge(id, acknowledgedAt = isoNow(this.now)) {
    toPositiveInteger(id, 'id');
    requireIsoTimestamp(acknowledgedAt, 'acknowledgedAt');
    const result = this.db
      .prepare(`
        UPDATE episodes
        SET status = 'acknowledged', acknowledged_at = ?
        WHERE id = ? AND status = 'open'
      `)
      .run(acknowledgedAt, id);
    return result.changes > 0;
  }
}
