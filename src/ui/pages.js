import { escapeHtml } from '../http/html.js';

export const EMAIL_STATUS_LABELS = Object.freeze({
  accepted: 'Accepté par le fournisseur',
  refused: 'Refusé',
  not_attempted: 'Non tenté',
});

const EPISODE_STATUS_LABELS = Object.freeze({
  open: 'À traiter',
  acknowledged: 'Acquitté',
});

const STYLES = `
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; line-height: 1.5; }
  header { border-bottom: 1px solid currentColor; }
  nav { max-width: 72rem; margin: 0 auto; padding: 0.75rem 1.25rem; display: flex; gap: 1.25rem; flex-wrap: wrap; }
  nav a { color: inherit; font-weight: 600; }
  main { max-width: 72rem; margin: 0 auto; padding: 1.5rem 1.25rem 3rem; }
  h1 { font-size: 1.6rem; margin-top: 0; }
  h2 { font-size: 1.15rem; margin-top: 2rem; }
  table { width: 100%; border-collapse: collapse; margin-top: 0.75rem; }
  th, td { text-align: left; vertical-align: top; padding: 0.45rem 0.6rem; border-bottom: 1px solid rgba(128, 128, 128, 0.45); }
  th { font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.03em; opacity: 0.8; }
  ul { padding-left: 1.25rem; margin: 0; }
  form { margin-top: 1rem; }
  fieldset { margin: 1rem 0; padding: 0.75rem 1rem; border: 1px solid rgba(128, 128, 128, 0.6); border-radius: 0.4rem; }
  legend { font-weight: 600; padding: 0 0.35rem; }
  label { display: block; font-weight: 600; margin-top: 0.75rem; }
  input, textarea { display: block; width: 100%; max-width: 42rem; font: inherit; padding: 0.45rem 0.55rem; margin-top: 0.25rem; }
  textarea { min-height: 7rem; resize: vertical; }
  button { font: inherit; padding: 0.55rem 1rem; cursor: pointer; margin-top: 1.25rem; }
  .banner { border: 2px solid currentColor; border-radius: 0.45rem; padding: 0.85rem 1rem; margin: 0 0 1.5rem; }
  .banner strong { display: block; margin-bottom: 0.25rem; }
  .banner p { margin: 0.25rem 0; }
  .notice { border-left: 0.3rem solid currentColor; padding: 0.5rem 0.9rem; margin: 1rem 0; }
  .notice-ok { border-left-style: solid; }
  .notice-warn { border-left-style: double; }
  .notice-error { border-left-style: solid; }
  .status { font-weight: 600; white-space: nowrap; }
  .muted { opacity: 0.75; }
  .reason { overflow-wrap: anywhere; }
  .rule-list { list-style: disc; }
`;

function formatTimestamp(value) {
  if (typeof value !== 'string') {
    return '—';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return '—';
  }
  return `${date.toISOString().replace('T', ' ').slice(0, 16)} UTC`;
}

function statusBadge(status) {
  const label = EMAIL_STATUS_LABELS[status] ?? 'État inconnu';
  return `<span class="status">${escapeHtml(label)}</span>`;
}

function episodeStatusLabel(status) {
  return EPISODE_STATUS_LABELS[status] ?? 'Inconnu';
}

function bannerMarkup(banner) {
  if (!banner || banner.visible !== true) {
    return '';
  }
  return `<div class="banner" role="alert">
<strong>Échecs d’envoi répétés</strong>
<p>${escapeHtml(banner.message ?? 'Des envois d’alerte par email ont échoué de façon répétée.')}</p>
<p><a href="/settings">Vérifier la configuration email</a></p>
</div>`;
}

function flashMarkup(flash) {
  if (!flash) {
    return '';
  }
  const kind = flash.kind === 'ok' ? 'ok' : flash.kind === 'error' ? 'error' : 'warn';
  return `<div class="notice notice-${kind}" role="status">${escapeHtml(flash.message)}</div>`;
}

function configurationMarkup(configuration) {
  if (!configuration) {
    return '';
  }
  if (configuration.complete) {
    return '<div class="notice notice-ok"><strong>Configuration email complète.</strong></div>';
  }
  const missing =
    Array.isArray(configuration.missing) && configuration.missing.length > 0
      ? configuration.missing.join(', ')
      : 'champs non renseignés';
  return `<div class="notice notice-warn">
<strong>Configuration email incomplète.</strong>
<p>Champs manquants : ${escapeHtml(missing)}.</p>
<p><a href="/settings">Compléter les réglages email</a></p>
</div>`;
}

function layout({ title, heading, csrfToken, banner, body }) {
  return `<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)}</title>
<style>${STYLES}</style>
</head>
<body>
<header>
<nav>
<a href="/">Tableau de bord</a>
<a href="/settings">Réglages email</a>
<a href="/episodes/new">Nouvel épisode</a>
</nav>
</header>
<main>
${bannerMarkup(banner)}
<h1>${escapeHtml(heading)}</h1>
${body}
</main>
</body>
</html>
`;
}

function csrfField(csrfToken) {
  if (typeof csrfToken !== 'string' || csrfToken === '') {
    return '';
  }
  return `<input type="hidden" name="_csrf" value="${escapeHtml(csrfToken)}">`;
}

function rulesCell(rules) {
  if (!Array.isArray(rules) || rules.length === 0) {
    return '—';
  }
  const items = rules
    .map((rule) => {
      const label = escapeHtml(rule.label ?? 'Règle sans libellé');
      return rule.detail
        ? `<li>${label}<br><span class="muted">${escapeHtml(rule.detail)}</span></li>`
        : `<li>${label}</li>`;
    })
    .join('\n');
  return `<ul class="rule-list">${items}</ul>`;
}

function acknowledgementCell(episode, { tokenSigner, now }) {
  if (episode.status !== 'open') {
    return '—';
  }
  if (
    typeof episode.ackNonce !== 'string' ||
    episode.ackNonce === '' ||
    typeof episode.ackTokenExpiresAt !== 'string'
  ) {
    return '—';
  }
  const expiresAt = Date.parse(episode.ackTokenExpiresAt);
  if (!Number.isFinite(expiresAt) || expiresAt <= now.getTime()) {
    return '<span class="muted">Lien expiré</span>';
  }
  let token;
  try {
    token = tokenSigner.issue({
      episodeId: episode.id,
      nonce: episode.ackNonce,
      expiresAt: episode.ackTokenExpiresAt,
    });
  } catch {
    return '—';
  }
  return `<a href="/ack/${escapeHtml(encodeURIComponent(token))}">Page d’acquittement</a>`;
}

function deliveryCell(delivery) {
  if (delivery && typeof delivery.status === 'string') {
    return statusBadge(delivery.status);
  }
  if (delivery && delivery.pending === true) {
    return '<span class="status">Indéterminé</span><br><span class="muted">Tentative réservée mais non terminée</span>';
  }
  return '<span class="muted">Aucune tentative</span>';
}

function episodesTable(episodes, options) {
  if (!Array.isArray(episodes) || episodes.length === 0) {
    return '<p class="muted">Aucun épisode enregistré.</p>';
  }
  const rows = episodes
    .map((episode) => {
      return `<tr>
<td>n° ${escapeHtml(episode.id)}</td>
<td>${escapeHtml(formatTimestamp(episode.openedAt))}</td>
<td>${escapeHtml(episode.embassyName ?? '—')}</td>
<td>${rulesCell(episode.rules)}</td>
<td>${escapeHtml(episodeStatusLabel(episode.status))}</td>
<td>${deliveryCell(episode.delivery)}</td>
<td>${acknowledgementCell(episode, options)}</td>
</tr>`;
    })
    .join('\n');
  return `<table>
<thead><tr><th>N°</th><th>Déclaré le</th><th>Ambassade</th><th>Règles déclenchées</th><th>Épisode</th><th>Envoi</th><th>Acquittement</th></tr></thead>
<tbody>
${rows}
</tbody>
</table>`;
}

function attemptsTable(attempts) {
  if (!Array.isArray(attempts) || attempts.length === 0) {
    return '<p class="muted">Aucune tentative d’envoi enregistrée.</p>';
  }
  const rows = attempts
    .map((attempt) => {
      const kind = attempt.kind === 'test' ? 'Test' : 'Alerte';
      const episode =
        attempt.kind === 'episode' && attempt.episodeId !== null ? `n° ${attempt.episodeId}` : '—';
      return `<tr>
<td>${escapeHtml(formatTimestamp(attempt.attemptedAt))}</td>
<td>${escapeHtml(kind)}</td>
<td>${escapeHtml(episode)}</td>
<td>${statusBadge(attempt.status)}</td>
<td class="reason">${escapeHtml(attempt.providerMessageId ?? '—')}</td>
<td class="reason">${escapeHtml(attempt.reason ?? '—')}</td>
</tr>`;
    })
    .join('\n');
  return `<table>
<thead><tr><th>Date</th><th>Type</th><th>Épisode</th><th>Statut</th><th>Identifiant fournisseur</th><th>Motif</th></tr></thead>
<tbody>
${rows}
</tbody>
</table>`;
}

export function renderDashboardPage({
  banner = null,
  flash = null,
  configuration = null,
  episodes = [],
  attempts = [],
  csrfToken = null,
  tokenSigner,
  now = new Date(),
} = {}) {
  const body = `${flashMarkup(flash)}
${configurationMarkup(configuration)}
<h2>Test de configuration</h2>
<p>Envoie un message de test au destinataire enregistré. Le résultat est conservé dans les tentatives d’envoi.</p>
<form method="post" action="/email/test">
${csrfField(csrfToken)}
<button type="submit">Envoyer un email de test</button>
</form>
<h2>Épisodes</h2>
${episodesTable(episodes, { tokenSigner, now })}
<h2>Tentatives d’envoi</h2>
<p class="muted">Une tentative est enregistrée par épisode, ou pour un test de configuration.</p>
${attemptsTable(attempts)}`;
  return layout({
    title: 'Tableau de bord — Alertes ambassade',
    heading: 'Tableau de bord',
    csrfToken,
    banner,
    body,
  });
}

function valueOf(values, name) {
  const value = values?.[name];
  return typeof value === 'string' ? value : '';
}

export function renderSettingsPage({
  banner = null,
  flash = null,
  configuration = null,
  values = {},
  csrfToken = null,
} = {}) {
  const apiKeyState = configuration?.apiKeyConfigured
    ? 'Une clé est enregistrée. Laissez le champ vide pour la conserver.'
    : 'Aucune clé n’est enregistrée.';
  const apiKeyError =
    configuration?.apiKeyError && configuration.apiKeyError !== ''
      ? `<p class="notice notice-warn">${escapeHtml(configuration.apiKeyError)}</p>`
      : '';
  const body = `${flashMarkup(flash)}
${configurationMarkup(configuration)}
<p>L’expéditeur et le destinataire sont uniques. Le sous-domaine d’envoi appartient à vlanp.com ; ses enregistrements DNS (SPF, DKIM, DMARC) sont à votre charge.</p>
${apiKeyError}
<form method="post" action="/settings">
${csrfField(csrfToken)}
<label for="sender">Expéditeur</label>
<input id="sender" name="sender" type="email" autocomplete="off" value="${escapeHtml(valueOf(values, 'sender'))}" placeholder="alertes@alertes.vlanp.com">
<label for="recipient">Destinataire</label>
<input id="recipient" name="recipient" type="email" autocomplete="off" value="${escapeHtml(valueOf(values, 'recipient'))}" placeholder="operateur@example.com">
<label for="sendingSubdomain">Sous-domaine d’envoi</label>
<input id="sendingSubdomain" name="sendingSubdomain" type="text" autocomplete="off" value="${escapeHtml(valueOf(values, 'sendingSubdomain'))}" placeholder="alertes">
<label for="apiKey">Clé API Resend</label>
<input id="apiKey" name="apiKey" type="password" autocomplete="new-password" value="">
<p class="muted">${escapeHtml(apiKeyState)} La clé n’est jamais affichée et n’est pas conservée dans la base de données.</p>
<button type="submit">Enregistrer les réglages</button>
</form>`;
  return layout({
    title: 'Réglages email — Alertes ambassade',
    heading: 'Réglages email',
    csrfToken,
    banner,
    body,
  });
}

const RULE_ROWS = 5;

function ruleValues(values, index) {
  const rules = Array.isArray(values?.rules) ? values.rules : [];
  const rule = rules[index];
  return {
    ruleKey: typeof rule?.ruleKey === 'string' ? rule.ruleKey : '',
    label: typeof rule?.label === 'string' ? rule.label : '',
    detail: typeof rule?.detail === 'string' ? rule.detail : '',
  };
}

function ruleFields(index, values) {
  const rule = ruleValues(values, index);
  const number = index + 1;
  return `<fieldset>
<legend>Règle ${number}</legend>
<label for="rule${index}Key">Clé de la règle</label>
<input id="rule${index}Key" name="rule${index}Key" type="text" autocomplete="off" value="${escapeHtml(rule.ruleKey)}">
<label for="rule${index}Label">Libellé</label>
<input id="rule${index}Label" name="rule${index}Label" type="text" autocomplete="off" value="${escapeHtml(rule.label)}">
<label for="rule${index}Detail">Détail (facultatif)</label>
<textarea id="rule${index}Detail" name="rule${index}Detail">${escapeHtml(rule.detail)}</textarea>
</fieldset>`;
}

export function renderNewEpisodePage({
  banner = null,
  flash = null,
  configuration = null,
  values = {},
  csrfToken = null,
} = {}) {
  const rules = Array.from({ length: RULE_ROWS }, (_, index) => ruleFields(index, values)).join('\n');
  const body = `${flashMarkup(flash)}
${configurationMarkup(configuration)}
<p>Chaque règle renseignée est regroupée dans un seul épisode. L’envoi d’alerte est tenté une seule fois pour cet épisode.</p>
<form method="post" action="/episodes">
${csrfField(csrfToken)}
<label for="embassyName">Ambassade</label>
<input id="embassyName" name="embassyName" type="text" autocomplete="off" value="${escapeHtml(valueOf(values, 'embassyName'))}" placeholder="Ambassade de France">
<label for="embassyPageUrl">Page de l’ambassade</label>
<input id="embassyPageUrl" name="embassyPageUrl" type="url" autocomplete="off" value="${escapeHtml(valueOf(values, 'embassyPageUrl'))}" placeholder="https://exemple.gouv.fr/page">
<label for="excerpt">Extrait du contenu modifié</label>
<textarea id="excerpt" name="excerpt" maxlength="20000">${escapeHtml(valueOf(values, 'excerpt'))}</textarea>
<label for="contentHash">Empreinte du contenu (facultatif)</label>
<input id="contentHash" name="contentHash" type="text" autocomplete="off" value="${escapeHtml(valueOf(values, 'contentHash'))}">
<h2>Règles déclenchées</h2>
<p class="muted">Renseignez au moins une règle. Les blocs entièrement vides sont ignorés.</p>
${rules}
<button type="submit">Déclarer l’épisode</button>
</form>`;
  return layout({
    title: 'Nouvel épisode — Alertes ambassade',
    heading: 'Nouvel épisode d’alerte',
    csrfToken,
    banner,
    body,
  });
}

export { RULE_ROWS };
