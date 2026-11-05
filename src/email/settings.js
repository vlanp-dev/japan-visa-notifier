import { randomBytes } from 'node:crypto';
import {
  chmodSync,
  closeSync,
  fsyncSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import path from 'node:path';

/**
 * Settings keys used for the email configuration. The Resend API key is
 * deliberately not one of them: it lives in a private file outside SQLite.
 */
export const EMAIL_SETTING_KEYS = Object.freeze({
  sender: 'email.sender',
  recipient: 'email.recipient',
  sendingSubdomain: 'email.sending_subdomain',
});

const FIELD_LABELS = Object.freeze({
  sender: 'expéditeur',
  recipient: 'destinataire',
  sendingSubdomain: 'sous-domaine d’envoi',
  apiKey: 'clé API Resend',
});

const EMAIL_ADDRESS = /^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$/;
const SUBDOMAIN = /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*$/;
const MAX_EMAIL_LENGTH = 254;
const MAX_SUBDOMAIN_LENGTH = 253;
const MAX_API_KEY_LENGTH = 512;
const CONTROL_CHARACTER = /[\u0000-\u001f\u007f]/;
const SENDING_DOMAIN = 'vlanp.com';

export class EmailConfigurationError extends Error {
  constructor(message, { code = 'EMAIL_CONFIGURATION_INVALID' } = {}) {
    super(message);
    this.name = 'EmailConfigurationError';
    this.code = code;
  }
}

/** Return the trimmed value, or null for absent and blank input. */
function optionalText(value, name) {
  if (value === undefined || value === null) {
    return null;
  }
  if (typeof value !== 'string') {
    throw new TypeError(`${name} doit être une chaîne de caractères.`);
  }
  const trimmed = value.trim();
  return trimmed === '' ? null : trimmed;
}

/** Validate one single email address. Never accepts a recipient list. */
function normalizeEmail(value, name) {
  if (value.length > MAX_EMAIL_LENGTH) {
    throw new TypeError(`${name} ne peut pas dépasser ${MAX_EMAIL_LENGTH} caractères.`);
  }
  if (/[\s,;<>]/.test(value) || !EMAIL_ADDRESS.test(value)) {
    throw new TypeError(`${name} doit être une adresse email unique et valide.`);
  }
  return value;
}

/** Validate a subdomain of vlanp.com (for example `alertes`). */
function normalizeSubdomain(value) {
  const normalized = value.toLowerCase();
  if (normalized.length > MAX_SUBDOMAIN_LENGTH || !SUBDOMAIN.test(normalized)) {
    throw new TypeError(
      'Le sous-domaine d’envoi doit être un nom DNS valide, sans schéma ni espace.',
    );
  }
  return normalized;
}

function isValidEmail(value) {
  return (
    typeof value === 'string' &&
    value.length <= MAX_EMAIL_LENGTH &&
    !/[\s,;<>]/.test(value) &&
    EMAIL_ADDRESS.test(value)
  );
}

function isValidSubdomain(value) {
  return (
    typeof value === 'string' &&
    value.length <= MAX_SUBDOMAIN_LENGTH &&
    SUBDOMAIN.test(value.toLowerCase())
  );
}

/**
 * The configured sender must belong to the configured sending subdomain of
 * vlanp.com. The subdomain may be written as `alertes` or `alertes.vlanp.com`.
 */
export function senderMatchesSendingSubdomain(sender, sendingSubdomain) {
  if (!isValidEmail(sender) || !isValidSubdomain(sendingSubdomain)) {
    return false;
  }
  const domain = sender.slice(sender.lastIndexOf('@') + 1).toLowerCase();
  const subdomain = sendingSubdomain.toLowerCase().replace(/\.+$/, '');
  return domain === subdomain || domain === `${subdomain}.${SENDING_DOMAIN}`;
}

/**
 * Validate a provider key without ever echoing it. The provider remains the
 * authority on whether the key is accepted; this only rejects values that
 * cannot safely be used or stored.
 */
function normalizeApiKey(value) {
  if (typeof value !== 'string') {
    throw new TypeError('La clé API Resend doit être une chaîne de caractères.');
  }
  const trimmed = value.trim();
  if (trimmed === '') {
    throw new TypeError('La clé API Resend ne peut pas être vide.');
  }
  if (
    trimmed.length > MAX_API_KEY_LENGTH ||
    CONTROL_CHARACTER.test(trimmed) ||
    /\s/.test(trimmed)
  ) {
    throw new TypeError('La clé API Resend contient des caractères invalides.');
  }
  return trimmed;
}

/**
 * Write a secret to a new private file and atomically replace the destination.
 * The temporary file is created with mode 0600, flushed, chmodded again after
 * the rename and removed if any step fails. No secret appears in errors.
 */
function writePrivateFile(filePath, contents) {
  const directory = path.dirname(filePath);
  const temporaryPath = path.join(
    directory,
    `.${path.basename(filePath)}.${process.pid}.${randomBytes(8).toString('hex')}.tmp`,
  );
  let descriptor = null;
  try {
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    descriptor = openSync(temporaryPath, 'wx', 0o600);
    writeFileSync(descriptor, contents, { encoding: 'utf8' });
    fsyncSync(descriptor);
    closeSync(descriptor);
    descriptor = null;
    chmodSync(temporaryPath, 0o600);
    renameSync(temporaryPath, filePath);
    chmodSync(filePath, 0o600);
  } catch {
    if (descriptor !== null) {
      try {
        closeSync(descriptor);
      } catch {
        // Preserve the original failure.
      }
    }
    try {
      unlinkSync(temporaryPath);
    } catch {
      // The temporary file may not exist.
    }
    throw new EmailConfigurationError(
      'L’enregistrement de la clé API Resend a échoué.',
      { code: 'EMAIL_API_KEY_WRITE_FAILED' },
    );
  }
}

/**
 * Persist the sender, the single recipient and the sending subdomain in the
 * settings table, and the Resend API key in a 0600 file outside the database.
 * Reading the key happens at send time; it is never returned by the public
 * configuration state and never appears in HTML, logs or error messages.
 */
export class EmailSettingsService {
  constructor({ repositories, apiKeyPath } = {}) {
    if (!repositories || !repositories.settings) {
      throw new TypeError('repositories with a settings repository are required.');
    }
    if (typeof apiKeyPath !== 'string' || apiKeyPath.trim() === '') {
      throw new TypeError('apiKeyPath must be a non-empty string.');
    }
    this.settings = repositories.settings;
    this.apiKeyPath = path.resolve(apiKeyPath);
  }

  /** Persisted, non-secret settings. The API key is never included. */
  getSettings() {
    const stored = this.settings.getAll();
    return {
      sender: stored[EMAIL_SETTING_KEYS.sender] ?? null,
      recipient: stored[EMAIL_SETTING_KEYS.recipient] ?? null,
      sendingSubdomain: stored[EMAIL_SETTING_KEYS.sendingSubdomain] ?? null,
    };
  }

  /**
   * Read the key from disk for one send. Returns null when the file is absent
   * or empty; raises a safe configuration error when it cannot be read.
   */
  readApiKey() {
    let raw;
    try {
      raw = readFileSync(this.apiKeyPath, 'utf8');
    } catch (error) {
      if (error && error.code === 'ENOENT') {
        return null;
      }
      throw new EmailConfigurationError(
        'La clé API Resend est illisible.',
        { code: 'EMAIL_API_KEY_UNREADABLE' },
      );
    }
    const value = raw.trim();
    return value === '' ? null : value;
  }

  /**
   * Report which fields are missing without contacting the provider. This is
   * safe to call on every dashboard load and on startup.
   */
  getConfigurationState() {
    const settings = this.getSettings();
    let apiKeyConfigured = false;
    let apiKeyError = null;
    try {
      apiKeyConfigured = this.readApiKey() !== null;
    } catch (error) {
      apiKeyError = error.message;
    }

    const missingFields = [];
    const invalidFields = [];
    const senderValid = isValidEmail(settings.sender);
    const recipientValid = isValidEmail(settings.recipient);
    const subdomainValid = isValidSubdomain(settings.sendingSubdomain);

    if (settings.sender === null) {
      missingFields.push('sender');
    } else if (!senderValid) {
      invalidFields.push('sender');
    }
    if (settings.recipient === null) {
      missingFields.push('recipient');
    } else if (!recipientValid) {
      invalidFields.push('recipient');
    }
    if (settings.sendingSubdomain === null) {
      missingFields.push('sendingSubdomain');
    } else if (!subdomainValid) {
      invalidFields.push('sendingSubdomain');
    }
    if (
      senderValid &&
      subdomainValid &&
      !senderMatchesSendingSubdomain(settings.sender, settings.sendingSubdomain)
    ) {
      invalidFields.push('sender');
    }
    if (!apiKeyConfigured) {
      missingFields.push('apiKey');
    }

    return {
      complete: missingFields.length === 0 && invalidFields.length === 0,
      missingFields,
      missing: missingFields.map((field) => FIELD_LABELS[field]),
      invalidFields,
      invalid: invalidFields.map((field) => FIELD_LABELS[field]),
      sender: settings.sender,
      recipient: settings.recipient,
      sendingSubdomain: settings.sendingSubdomain,
      apiKeyConfigured,
      apiKeyError,
    };
  }

  /** Convenience predicate derived from the same state. */
  isConfigured() {
    return this.getConfigurationState().complete;
  }

  /**
   * Update the configuration. Absent and blank fields keep their stored value,
   * so the settings form can leave the masked key untouched. Throws on invalid
   * input before writing anything.
   */
  update(input = {}) {
    if (input === null || typeof input !== 'object' || Array.isArray(input)) {
      throw new TypeError('La configuration email doit être un objet.');
    }

    const sender = optionalText(input.sender, 'sender');
    const recipient = optionalText(input.recipient, 'recipient');
    const sendingSubdomain = optionalText(input.sendingSubdomain, 'sendingSubdomain');
    const apiKey = optionalText(input.apiKey, 'apiKey');

    const normalizedSender = sender === null ? null : normalizeEmail(sender, 'L’expéditeur');
    const normalizedRecipient =
      recipient === null ? null : normalizeEmail(recipient, 'Le destinataire');
    const normalizedSubdomain =
      sendingSubdomain === null ? null : normalizeSubdomain(sendingSubdomain);

    // Validate the sender and the sending subdomain together before writing
    // anything: the subdomain must actually govern the From domain, otherwise
    // "configuration complete" would be misleading.
    const stored = this.getSettings();
    const effectiveSender = normalizedSender ?? stored.sender;
    const effectiveSubdomain = normalizedSubdomain ?? stored.sendingSubdomain;
    if (
      isValidEmail(effectiveSender) &&
      isValidSubdomain(effectiveSubdomain) &&
      !senderMatchesSendingSubdomain(effectiveSender, effectiveSubdomain)
    ) {
      throw new TypeError(
        `L’expéditeur doit appartenir au sous-domaine d’envoi (${effectiveSubdomain}.${SENDING_DOMAIN} ou ${effectiveSubdomain}).`,
      );
    }

    const updates = {};
    if (normalizedSender !== null) {
      updates[EMAIL_SETTING_KEYS.sender] = normalizedSender;
    }
    if (normalizedRecipient !== null) {
      updates[EMAIL_SETTING_KEYS.recipient] = normalizedRecipient;
    }
    if (normalizedSubdomain !== null) {
      updates[EMAIL_SETTING_KEYS.sendingSubdomain] = normalizedSubdomain;
    }

    if (apiKey !== null) {
      const normalizedKey = normalizeApiKey(apiKey);
      writePrivateFile(this.apiKeyPath, `${normalizedKey}\n`);
    }
    if (Object.keys(updates).length > 0) {
      this.settings.setMany(updates);
    }
    return this.getConfigurationState();
  }
}

export { FIELD_LABELS as EMAIL_CONFIGURATION_FIELD_LABELS };
