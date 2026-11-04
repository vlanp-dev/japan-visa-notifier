import { AppSecretsRepository } from './app-secrets.js';
import { EmailAttemptsRepository } from './email-attempts.js';
import { EmailDeliveryClaimsRepository } from './email-delivery-claims.js';
import { EpisodeRulesRepository } from './episode-rules.js';
import { EpisodesRepository } from './episodes.js';
import { SettingsRepository } from './settings.js';

export function createRepositories(db, { now } = {}) {
  const options = now === undefined ? {} : { now };
  const emailAttempts = new EmailAttemptsRepository(db, options);
  return {
    settings: new SettingsRepository(db, options),
    appSecrets: new AppSecretsRepository(db, options),
    episodes: new EpisodesRepository(db, options),
    episodeRules: new EpisodeRulesRepository(db),
    emailAttempts,
    emailDeliveryClaims: new EmailDeliveryClaimsRepository(db, {
      ...options,
      emailAttempts,
    }),
  };
}

export {
  AppSecretsRepository,
  EmailAttemptsRepository,
  EmailDeliveryClaimsRepository,
  EpisodeRulesRepository,
  EpisodesRepository,
  SettingsRepository,
};
