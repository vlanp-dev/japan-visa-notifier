/**
 * Application-level use case: open one alert episode and make its mandatory
 * single email attempt. Every producer of alert episodes must call
 * `openAndNotify` rather than opening an episode directly, so the durable
 * delivery intent and the provider attempt are never skipped.
 *
 * `EpisodeService.openEpisode` already commits the delivery intent (an
 * unstarted claim) in the same transaction as the episode. This class adds the
 * attempt itself and returns both halves of the result to its caller.
 */
export class AlertEpisodeService {
  constructor({ episodeService, emailService } = {}) {
    if (!episodeService || typeof episodeService.openEpisode !== 'function') {
      throw new TypeError('episodeService with openEpisode is required.');
    }
    if (!emailService || typeof emailService.deliverEpisode !== 'function') {
      throw new TypeError('emailService with deliverEpisode is required.');
    }
    this.episodeService = episodeService;
    this.emailService = emailService;
  }

  async openAndNotify(input = {}) {
    const opened = this.episodeService.openEpisode(input);
    const delivery = await this.emailService.deliverEpisode(opened);
    return { ...opened, delivery };
  }
}

export function createAlertEpisodeService(options) {
  return new AlertEpisodeService(options);
}
