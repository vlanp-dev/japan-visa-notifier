import { EmailConfigurationError } from '../email/settings.js';
import { safeHttpUrl } from '../http/html.js';
import {
  RULE_ROWS,
  renderDashboardPage,
  renderNewEpisodePage,
  renderSettingsPage,
} from './pages.js';

const EPISODE_HISTORY_LIMIT = 50;
const ATTEMPT_HISTORY_LIMIT = 50;

function formText(form, name) {
  const value = form?.[name];
  return typeof value === 'string' ? value : '';
}

function readRuleRows(form) {
  const rules = [];
  for (let index = 0; index < RULE_ROWS; index += 1) {
    const ruleKey = formText(form, `rule${index}Key`).trim();
    const label = formText(form, `rule${index}Label`).trim();
    const detail = formText(form, `rule${index}Detail`).trim();
    if (ruleKey === '' && label === '' && detail === '') {
      continue;
    }
    if (ruleKey === '' || label === '') {
      return {
        error: `Règle ${index + 1} : la clé et le libellé sont obligatoires.`,
      };
    }
    rules.push(detail === '' ? { ruleKey, label } : { ruleKey, label, detail });
  }

  if (rules.length === 0) {
    return { error: 'Ajoutez au moins une règle déclenchée.' };
  }
  const seen = new Set();
  for (const rule of rules) {
    if (seen.has(rule.ruleKey)) {
      return { error: `La clé de règle « ${rule.ruleKey} » est utilisée plusieurs fois.` };
    }
    seen.add(rule.ruleKey);
  }
  return { rules };
}

function readEpisodeForm(form) {
  const values = {
    embassyName: formText(form, 'embassyName'),
    embassyPageUrl: formText(form, 'embassyPageUrl'),
    excerpt: formText(form, 'excerpt'),
    contentHash: formText(form, 'contentHash'),
    rules: [],
  };
  for (let index = 0; index < RULE_ROWS; index += 1) {
    values.rules.push({
      ruleKey: formText(form, `rule${index}Key`),
      label: formText(form, `rule${index}Label`),
      detail: formText(form, `rule${index}Detail`),
    });
  }

  const parsedRules = readRuleRows(form);
  if (parsedRules.error) {
    return { values, error: parsedRules.error };
  }
  const embassyPageUrl = values.embassyPageUrl.trim();
  if (embassyPageUrl !== '' && safeHttpUrl(embassyPageUrl) === null) {
    return {
      values,
      error: 'La page de l’ambassade doit être une URL absolue http(s) sans identifiants.',
    };
  }

  return {
    values,
    input: {
      embassyName: values.embassyName,
      embassyPageUrl: values.embassyPageUrl,
      excerpt: values.excerpt,
      contentHash: values.contentHash,
      rules: parsedRules.rules,
    },
  };
}

function settingsDisplayValues(submitted, stored) {
  const display = {};
  for (const field of ['sender', 'recipient', 'sendingSubdomain']) {
    display[field] =
      typeof submitted[field] === 'string' && submitted[field].trim() !== ''
        ? submitted[field]
        : (stored?.[field] ?? '');
  }
  return display;
}

function episodeErrorMessage(error) {
  if (error instanceof TypeError) {
    return 'Les données de l’épisode sont invalides. Vérifiez les champs et leur longueur.';
  }
  return 'L’épisode n’a pas été déclaré à cause d’une erreur interne.';
}

function settingsErrorMessage(error) {
  if (error instanceof EmailConfigurationError || error instanceof TypeError) {
    return error.message;
  }
  return 'Les réglages n’ont pas été enregistrés à cause d’une erreur interne.';
}

function testFlash(status) {
  if (status === 'accepted') {
    return { kind: 'ok', message: 'Test d’envoi : accepté par le fournisseur.' };
  }
  if (status === 'refused') {
    return {
      kind: 'warn',
      message: 'Test d’envoi : refusé. Le motif du fournisseur est enregistré ci-dessous.',
    };
  }
  if (status === 'not_attempted') {
    return {
      kind: 'warn',
      message: 'Test d’envoi : non tenté. La configuration email est incomplète.',
    };
  }
  return { kind: 'warn', message: 'Test d’envoi : état indéterminé.' };
}

function dashboardFlash(searchParams) {
  const test = searchParams.get('test');
  if (test !== null) {
    return testFlash(test);
  }

  const opened = searchParams.get('opened');
  if (opened === null || !/^\d{1,18}$/.test(opened)) {
    return null;
  }
  const delivery = searchParams.get('delivery');
  let message = `Épisode n° ${opened} déclaré.`;
  if (delivery === 'accepted') {
    message += ' Envoi : accepté par le fournisseur.';
  } else if (delivery === 'refused') {
    message += ' Envoi : refusé. Le motif du fournisseur est enregistré ci-dessous.';
  } else if (delivery === 'not_attempted') {
    message += ' Envoi : non tenté. La configuration email est incomplète.';
  } else {
    message += ' Envoi : état indéterminé.';
  }
  return { kind: delivery === 'accepted' ? 'ok' : 'warn', message };
}

/**
 * Operator interface in French. Every route inherits Basic auth and CSRF from
 * the HTTP application; only the signed acknowledgement links are public.
 */
export function registerOperatorUiRoutes(
  app,
  { repositories, episodeService, emailService, emailSettings, tokenSigner } = {},
) {
  if (
    !repositories ||
    !repositories.episodes ||
    !repositories.emailAttempts ||
    !episodeService ||
    !emailService ||
    !emailSettings ||
    !tokenSigner
  ) {
    throw new TypeError(
      'registerOperatorUiRoutes requires repositories, episodeService, emailService, emailSettings and tokenSigner.',
    );
  }

  function banner() {
    return emailService.getBannerState();
  }

  function configuration() {
    return emailSettings.getConfigurationState();
  }

  function dashboardData() {
    const attempts = repositories.emailAttempts.list({ limit: ATTEMPT_HISTORY_LIMIT });
    const episodes = repositories.episodes
      .list({ limit: EPISODE_HISTORY_LIMIT })
      .map((episode) => ({
        ...episode,
        rules: repositories.episodes.getRules(episode.id),
        attempt: repositories.emailAttempts.findByEpisodeId(episode.id),
      }));
    return { episodes, attempts };
  }

  function currentTime() {
    const now = typeof tokenSigner.now === 'function' ? tokenSigner.now() : new Date();
    return now instanceof Date && !Number.isNaN(now.getTime()) ? now : new Date();
  }

  app.get('/', (ctx) => {
    const { episodes, attempts } = dashboardData();
    ctx.sendHtml(
      200,
      renderDashboardPage({
        banner: banner(),
        flash: dashboardFlash(ctx.url.searchParams),
        configuration: configuration(),
        episodes,
        attempts,
        csrfToken: ctx.csrfToken,
        tokenSigner,
        now: currentTime(),
      }),
    );
  });

  app.get('/settings', (ctx) => {
    ctx.sendHtml(
      200,
      renderSettingsPage({
        banner: banner(),
        flash:
          ctx.url.searchParams.get('saved') === '1'
            ? { kind: 'ok', message: 'Réglages enregistrés.' }
            : null,
        configuration: configuration(),
        values: emailSettings.getSettings(),
        csrfToken: ctx.csrfToken,
      }),
    );
  });

  app.post('/settings', (ctx) => {
    const form = ctx.form ?? {};
    const submitted = {
      sender: formText(form, 'sender'),
      recipient: formText(form, 'recipient'),
      sendingSubdomain: formText(form, 'sendingSubdomain'),
    };
    try {
      emailSettings.update({ ...submitted, apiKey: formText(form, 'apiKey') });
    } catch (error) {
      ctx.logger.warn('operator_settings_rejected', { errorName: error?.name ?? 'Error' });
      ctx.sendHtml(
        422,
        renderSettingsPage({
          banner: banner(),
          flash: { kind: 'error', message: settingsErrorMessage(error) },
          configuration: configuration(),
          values: settingsDisplayValues(submitted, emailSettings.getSettings()),
          csrfToken: ctx.csrfToken,
        }),
      );
      return;
    }
    ctx.redirect('/settings?saved=1');
  });

  app.get('/episodes/new', (ctx) => {
    ctx.sendHtml(
      200,
      renderNewEpisodePage({
        banner: banner(),
        configuration: configuration(),
        values: {},
        csrfToken: ctx.csrfToken,
      }),
    );
  });

  app.post('/episodes', async (ctx) => {
    const parsed = readEpisodeForm(ctx.form ?? {});
    if (parsed.error) {
      ctx.sendHtml(
        400,
        renderNewEpisodePage({
          banner: banner(),
          configuration: configuration(),
          values: parsed.values,
          csrfToken: ctx.csrfToken,
          flash: { kind: 'error', message: parsed.error },
        }),
      );
      return;
    }

    let opened;
    try {
      opened = episodeService.openEpisode(parsed.input);
    } catch (error) {
      ctx.logger.warn('operator_episode_rejected', { errorName: error?.name ?? 'Error' });
      ctx.sendHtml(
        400,
        renderNewEpisodePage({
          banner: banner(),
          configuration: configuration(),
          values: parsed.values,
          csrfToken: ctx.csrfToken,
          flash: { kind: 'error', message: episodeErrorMessage(error) },
        }),
      );
      return;
    }

    const delivery = await emailService.deliverEpisode(opened);
    const status = typeof delivery?.status === 'string' ? delivery.status : 'pending';
    ctx.logger.info('operator_episode_opened', {
      episodeId: opened.episode.id,
      deliveryStatus: status,
    });
    ctx.redirect(`/?opened=${opened.episode.id}&delivery=${encodeURIComponent(status)}`);
  });

  app.post('/email/test', async (ctx) => {
    const result = await emailService.sendTestEmail();
    const status = typeof result?.status === 'string' ? result.status : 'pending';
    ctx.logger.info('operator_test_email_requested', { deliveryStatus: status });
    ctx.redirect(`/?test=${encodeURIComponent(status)}`);
  });
}
