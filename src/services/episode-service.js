import { randomBytes, randomUUID } from 'node:crypto';
import { safeHttpUrl } from '../http/html.js';
import { isoNow, requireNonEmptyString } from '../repositories/validation.js';

const MAX_RULES = 100;
const MAX_RULE_KEY_LENGTH = 120;
const MAX_RULE_LABEL_LENGTH = 300;
const MAX_RULE_DETAIL_LENGTH = 4000;
const MAX_EMBASSY_NAME_LENGTH = 300;
const MAX_PAGE_URL_LENGTH = 2048;
const MAX_EXCERPT_LENGTH = 20000;
const MAX_CONTENT_HASH_LENGTH = 200;

function optionalText(value, name, maxLength) {
  if (value === undefined || value === null) {
    return null;
  }
  if (typeof value !== 'string') {
    throw new TypeError(`${name} must be a string or null.`);
  }
  const trimmed = value.trim();
  if (trimmed === '') {
    return null;
  }
  if (trimmed.length > maxLength) {
    throw new TypeError(`${name} must be at most ${maxLength} characters.`);
  }
  return trimmed;
}

function pageUrl(value) {
  if (value === undefined || value === null) {
    return null;
  }
  if (typeof value !== 'string') {
    throw new TypeError('embassyPageUrl must be a string or null.');
  }
  const trimmed = value.trim();
  if (trimmed === '') {
    return null;
  }
  if (trimmed.length > MAX_PAGE_URL_LENGTH || safeHttpUrl(trimmed) === null) {
    throw new TypeError('embassyPageUrl must be an absolute http(s) URL without credentials.');
  }
  return trimmed;
}

function normalizeRule(rule, index) {
  if (rule === null || typeof rule !== 'object' || Array.isArray(rule)) {
    throw new TypeError(`rules[${index}] must be an object.`);
  }
  const ruleKey = requireNonEmptyString(rule.ruleKey, `rules[${index}].ruleKey`);
  const label = requireNonEmptyString(rule.label, `rules[${index}].label`);
  if (ruleKey.length > MAX_RULE_KEY_LENGTH) {
    throw new TypeError(`rules[${index}].ruleKey must be at most ${MAX_RULE_KEY_LENGTH} characters.`);
  }
  if (label.length > MAX_RULE_LABEL_LENGTH) {
    throw new TypeError(`rules[${index}].label must be at most ${MAX_RULE_LABEL_LENGTH} characters.`);
  }
  const detail = optionalText(rule.detail, `rules[${index}].detail`, MAX_RULE_DETAIL_LENGTH);
  return { ruleKey, label, detail };
}

function normalizeRules(rules) {
  if (!Array.isArray(rules) || rules.length === 0) {
    throw new TypeError('rules must be a non-empty array.');
  }
  if (rules.length > MAX_RULES) {
    throw new TypeError(`rules must contain at most ${MAX_RULES} entries.`);
  }
  const normalized = rules.map(normalizeRule);
  const seen = new Set();
  for (const rule of normalized) {
    if (seen.has(rule.ruleKey)) {
      throw new TypeError(`Duplicate rule key: ${rule.ruleKey}`);
    }
    seen.add(rule.ruleKey);
  }
  return normalized;
}

/**
 * Open alert episodes and acknowledge them through signed links. Opening is
 * validated here, at the trust boundary, before one atomic repository call
 * persists the episode, all triggered rules and the acknowledgement nonce.
 */
export class EpisodeService {
  constructor({ repositories, tokenSigner, now = () => new Date(), generateNonce } = {}) {
    if (!repositories || !repositories.episodes) {
      throw new TypeError('repositories with an episodes repository are required.');
    }
    if (!tokenSigner) {
      throw new TypeError('tokenSigner is required.');
    }
    if (typeof now !== 'function') {
      throw new TypeError('now must be a function.');
    }
    if (generateNonce !== undefined && typeof generateNonce !== 'function') {
      throw new TypeError('generateNonce must be a function.');
    }
    this.repositories = repositories;
    this.tokenSigner = tokenSigner;
    this.now = now;
    this.generateNonce = generateNonce ?? (() => randomBytes(16).toString('base64url'));
  }

  /** Validate, persist and return one episode with its signed ack link. */
  openEpisode(input = {}) {
    const embassyName = optionalText(input.embassyName, 'embassyName', MAX_EMBASSY_NAME_LENGTH);
    const embassyPageUrl = pageUrl(input.embassyPageUrl);
    if (embassyPageUrl === null) {
      throw new TypeError('embassyPageUrl is required for an alert episode.');
    }
    if (typeof input.excerpt !== 'string') {
      throw new TypeError('excerpt must be a string.');
    }
    if (input.excerpt.trim() === '') {
      throw new TypeError('excerpt must not be empty for an alert episode.');
    }
    const excerpt = input.excerpt;
    if (excerpt.length > MAX_EXCERPT_LENGTH) {
      throw new TypeError(`excerpt must be at most ${MAX_EXCERPT_LENGTH} characters.`);
    }
    const contentHash = optionalText(input.contentHash, 'contentHash', MAX_CONTENT_HASH_LENGTH);
    const rules = normalizeRules(input.rules);
    const openedAt = isoNow(this.now);
    const nonce = this.generateNonce();
    if (typeof nonce !== 'string' || nonce === '') {
      throw new TypeError('generateNonce must return a non-empty string.');
    }
    const expiresAt = this.tokenSigner.nextExpiry();
    // The delivery intent is committed with the episode. A crash before the
    // provider call therefore leaves an explicit pending claim, never an open
    // episode with no trace of the mandatory notification.
    const deliveryClaimToken = randomUUID();

    const stored = this.repositories.episodes.createWithRules({
      openedAt,
      embassyName,
      embassyPageUrl,
      excerpt,
      contentHash,
      ackNonce: nonce,
      ackTokenExpiresAt: expiresAt,
      deliveryClaimToken,
      rules,
    });
    const token = this.tokenSigner.issue({ episodeId: stored.id, nonce, expiresAt });
    return {
      episode: this.repositories.episodes.getWithRules(stored.id),
      token,
      acknowledgementUrl: this.tokenSigner.acknowledgementUrl(token),
      deliveryClaimToken,
      expiresAt,
    };
  }

  /**
   * Read-only acknowledgement preview. A valid token, a matching stored nonce
   * and an open episode are required; nothing is written.
   */
  inspectAcknowledgement(token) {
    const resolved = this.resolveToken(token);
    if (!resolved.ok) {
      return resolved;
    }
    return { ok: true, episode: resolved.episode };
  }

  /**
   * Re-verify signature, expiry and nonce, then transition open to
   * acknowledged. A replay or a concurrent second confirmation changes nothing.
   */
  acknowledge(token) {
    const resolved = this.resolveToken(token);
    if (!resolved.ok) {
      return resolved;
    }
    const changed = this.repositories.episodes.acknowledge(resolved.episode.id, isoNow(this.now));
    if (!changed) {
      const current = this.repositories.episodes.getById(resolved.episode.id);
      if (current === null) {
        return { ok: false, reason: 'not_found' };
      }
      return { ok: false, reason: 'already_acknowledged', episode: current };
    }
    return { ok: true, episode: this.repositories.episodes.getWithRules(resolved.episode.id) };
  }

  resolveToken(token) {
    const verification = this.tokenSigner.verify(token);
    if (!verification.ok) {
      return { ok: false, reason: verification.reason };
    }
    const episode = this.repositories.episodes.getWithRules(verification.episodeId);
    if (episode === null) {
      return { ok: false, reason: 'not_found' };
    }
    if (episode.status !== 'open') {
      return { ok: false, reason: 'already_acknowledged', episode };
    }
    if (
      typeof episode.ackNonce !== 'string' ||
      episode.ackNonce === '' ||
      episode.ackNonce !== verification.nonce ||
      episode.ackTokenExpiresAt !== verification.expiresAt
    ) {
      return { ok: false, reason: 'mismatch' };
    }
    return { ok: true, episode };
  }
}

export function createEpisodeService(options) {
  return new EpisodeService(options);
}
