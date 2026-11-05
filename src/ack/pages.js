import { escapeHtml, safeHttpUrl } from '../http/html.js';

const STYLES = `
  :root { color-scheme: light dark; }
  body { margin: 0; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; line-height: 1.5; }
  main { max-width: 44rem; margin: 0 auto; padding: 2rem 1.25rem 3rem; }
  h1 { font-size: 1.6rem; margin-top: 0; }
  h2 { font-size: 1.1rem; margin-top: 1.75rem; }
  dl { display: grid; grid-template-columns: max-content 1fr; gap: 0.35rem 1rem; }
  dt { font-weight: 600; }
  dd { margin: 0; overflow-wrap: anywhere; }
  ul { padding-left: 1.25rem; }
  pre { padding: 0.75rem; border: 1px solid currentColor; border-radius: 0.35rem; white-space: pre-wrap; overflow-wrap: anywhere; }
  .actions { margin-top: 2rem; }
  button { font: inherit; padding: 0.6rem 1.1rem; cursor: pointer; }
  .notice { border-left: 0.3rem solid currentColor; padding: 0.5rem 0.9rem; margin: 1.5rem 0; }
  .muted { opacity: 0.75; }
`;

function page({ title, heading, body }) {
  return `<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)}</title>
<style>${STYLES}</style>
</head>
<body>
<main>
<h1>${escapeHtml(heading)}</h1>
${body}
</main>
</body>
</html>
`;
}

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

function statusLabel(status) {
  if (status === 'open') {
    return 'Ouvert';
  }
  if (status === 'acknowledged') {
    return 'Acquitté';
  }
  return 'Inconnu';
}

function embassyLine(episode) {
  const url = safeHttpUrl(episode.embassyPageUrl);
  if (url === null) {
    return escapeHtml(episode.embassyPageUrl ?? '—');
  }
  return `<a href="${escapeHtml(url)}" rel="noreferrer">${escapeHtml(url)}</a>`;
}

function episodeSummary(episode) {
  return `<dl>
<dt>Épisode</dt><dd>n° ${escapeHtml(episode.id)}</dd>
<dt>Ambassade</dt><dd>${escapeHtml(episode.embassyName ?? 'Non renseignée')}</dd>
<dt>Page</dt><dd>${embassyLine(episode)}</dd>
<dt>Ouverture</dt><dd>${escapeHtml(formatTimestamp(episode.openedAt))}</dd>
<dt>État</dt><dd>${escapeHtml(statusLabel(episode.status))}</dd>
</dl>`;
}

function rulesSection(rules) {
  if (!Array.isArray(rules) || rules.length === 0) {
    return '';
  }
  const items = rules
    .map((rule) => {
      const detail = rule.detail ? `<br><span class="muted">${escapeHtml(rule.detail)}</span>` : '';
      return `<li><strong>${escapeHtml(rule.label)}</strong>${detail}</li>`;
    })
    .join('\n');
  return `<h2>Règles déclenchées</h2>\n<ul>\n${items}\n</ul>`;
}

export function renderAckConfirmationPage({ episode }) {
  const excerpt = episode.excerpt === '' ? '<p class="muted">Aucun extrait enregistré.</p>' : `<pre>${escapeHtml(episode.excerpt)}</pre>`;
  const body = `${episodeSummary(episode)}
${rulesSection(episode.rules)}
<h2>Extrait du contenu modifié</h2>
${excerpt}
<div class="notice">
<p>Ce lien est signé et ne peut acquitter que cet épisode. Consultez d’abord la page de l’ambassade avant de confirmer.</p>
</div>
<form class="actions" method="post" action="">
<button type="submit">Acquitter cet épisode</button>
</form>
<p class="muted">L’ouverture de cette page n’a rien modifié. Seul le bouton d’acquittement enregistre la décision.</p>`;
  return page({ title: 'Acquitter l’alerte', heading: 'Acquitter l’alerte', body });
}

export function renderAckSuccessPage({ episode }) {
  const body = `${episodeSummary(episode)}
<div class="notice">
<p>L’épisode est maintenant marqué comme acquitté. Aucune autre action n’est nécessaire.</p>
</div>
<p>Acquitté le ${escapeHtml(formatTimestamp(episode.acknowledgedAt))}.</p>
<p><a href="/">Retour au service</a></p>`;
  return page({ title: 'Épisode acquitté', heading: 'Épisode acquitté', body });
}

export function renderAckAlreadyAcknowledgedPage({ episode }) {
  const body = `${episodeSummary(episode)}
<div class="notice">
<p>Cet épisode a déjà été acquitté le ${escapeHtml(formatTimestamp(episode.acknowledgedAt))}. Aucune modification n’a été effectuée.</p>
</div>
<p><a href="/">Retour au service</a></p>`;
  return page({ title: 'Épisode déjà acquitté', heading: 'Épisode déjà acquitté', body });
}

export function renderAckErrorPage({ title, message }) {
  const body = `<div class="notice">
<p>${escapeHtml(message)}</p>
</div>
<p class="muted">Aucune modification n’a été effectuée.</p>
<p><a href="/">Retour au service</a></p>`;
  return page({ title, heading: title, body });
}
