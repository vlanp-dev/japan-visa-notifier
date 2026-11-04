import { requireNonEmptyString, requireOptionalString, requireIsoTimestamp, toPositiveInteger } from './validation.js';

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

export class EpisodeRulesRepository {
  constructor(db) {
    this.db = db;
  }

  listByEpisode(episodeId) {
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

  /**
   * Insert a rule for an existing episode. Most callers should use
   * EpisodesRepository.createWithRules so the episode and its rules commit
   * atomically; this exists for repair/import tooling and tests.
   */
  create(episodeId, { ruleKey, label, detail = null, createdAt = new Date().toISOString() }) {
    toPositiveInteger(episodeId, 'episodeId');
    requireNonEmptyString(ruleKey, 'ruleKey');
    requireNonEmptyString(label, 'label');
    requireOptionalString(detail, 'detail');
    requireIsoTimestamp(createdAt, 'createdAt');
    const result = this.db
      .prepare(`
        INSERT INTO episode_rules (episode_id, rule_key, label, detail, created_at)
        VALUES (?, ?, ?, ?, ?)
      `)
      .run(episodeId, ruleKey, label, detail, createdAt);
    const row = this.db
      .prepare('SELECT * FROM episode_rules WHERE id = ?')
      .get(Number(result.lastInsertRowid));
    return rowToRule(row);
  }
}
