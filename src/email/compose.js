import { escapeHtml, safeHttpUrl } from '../http/html.js';

const MAX_SUBJECT_LENGTH = 180;

function formatTimestamp(value) {
  if (typeof value !== 'string') {
    return 'date inconnue';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return 'date inconnue';
  }
  return `${date.toISOString().replace('T', ' ').slice(0, 16)} UTC`;
}

/** Collapse header-breaking whitespace and bound the subject length. */
function subject(value) {
  return value
    .replace(/[\r\n]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, MAX_SUBJECT_LENGTH);
}

function requireHttpUrl(value, name) {
  const url = safeHttpUrl(value);
  if (url === null) {
    throw new TypeError(`${name} doit être une URL absolue http(s) sans identifiants.`);
  }
  return url;
}

function rulesHtml(rules) {
  if (rules.length === 0) {
    return '<p>Aucune règle détaillée n’est associée à cet épisode.</p>';
  }
  const items = rules
    .map((rule) => {
      const label = escapeHtml(rule?.label ?? 'Règle sans libellé');
      const detail = rule?.detail
        ? `<br><span style="opacity: 0.8;">${escapeHtml(rule.detail)}</span>`
        : '';
      return `<li><strong>${label}</strong>${detail}</li>`;
    })
    .join('\n');
  return `<ul>\n${items}\n</ul>`;
}

function rulesText(rules) {
  if (rules.length === 0) {
    return 'Aucune règle détaillée n’est associée à cet épisode.';
  }
  return rules
    .map((rule) => {
      const label = rule?.label ?? 'Règle sans libellé';
      return rule?.detail ? `- ${label} — ${rule.detail}` : `- ${label}`;
    })
    .join('\n');
}

function excerptHtml(excerpt) {
  if (excerpt.trim() === '') {
    return '<p>Aucun extrait n’a été enregistré.</p>';
  }
  return `<pre style="margin: 0; padding: 0.75rem; border: 1px solid #999; white-space: pre-wrap; overflow-wrap: anywhere;">${escapeHtml(excerpt)}</pre>`;
}

function embassyHtml(embassyPageUrl) {
  const url = safeHttpUrl(embassyPageUrl);
  if (url === null) {
    return '<span>Aucune URL valide enregistrée.</span>';
  }
  return `<a href="${escapeHtml(url)}" rel="noreferrer">${escapeHtml(url)}</a>`;
}

function documentHtml({ title, body }) {
  return `<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)}</title>
</head>
<body style="margin: 0; padding: 0; font-family: system-ui, -apple-system, 'Segoe UI', sans-serif; line-height: 1.5;">
<main style="max-width: 44rem; margin: 0 auto; padding: 1.5rem;">
${body}
</main>
</body>
</html>
`;
}

/**
 * Compose the French alert email for one episode. Every triggered rule, the
 * modified excerpt, the embassy page URL and the signed acknowledgement URL
 * are present in both the HTML and the plain-text part. All interpolated
 * values are escaped and every URL must be an absolute http(s) URL.
 */
export function composeAlertEmail({ episode, acknowledgementUrl } = {}) {
  if (episode === null || typeof episode !== 'object' || Array.isArray(episode)) {
    throw new TypeError('episode doit être un objet.');
  }
  if (!Number.isInteger(episode.id) || episode.id < 1) {
    throw new TypeError('episode.id doit être un entier positif.');
  }
  const ackUrl = requireHttpUrl(acknowledgementUrl, 'acknowledgementUrl');
  const rules = Array.isArray(episode.rules) ? episode.rules : [];
  const excerpt = typeof episode.excerpt === 'string' ? episode.excerpt : '';
  const embassyName =
    typeof episode.embassyName === 'string' && episode.embassyName.trim() !== ''
      ? episode.embassyName.trim()
      : 'page surveillée';
  const embassyUrl = safeHttpUrl(episode.embassyPageUrl);
  const openedAt = formatTimestamp(episode.openedAt);

  const html = documentHtml({
    title: `Alerte ambassade — épisode n° ${episode.id}`,
    body: `<h1 style="font-size: 1.5rem; margin-top: 0;">Alerte ambassade</h1>
<p>Un épisode d’alerte a été déclenché le ${escapeHtml(openedAt)} pour <strong>${escapeHtml(embassyName)}</strong>.</p>

<h2 style="font-size: 1.1rem;">Règles déclenchées</h2>
${rulesHtml(rules)}

<h2 style="font-size: 1.1rem;">Extrait du contenu modifié</h2>
${excerptHtml(excerpt)}

<h2 style="font-size: 1.1rem;">Liens</h2>
<ul>
<li>Page de l’ambassade : ${embassyHtml(episode.embassyPageUrl)}</li>
<li>Acquittement : <a href="${escapeHtml(ackUrl)}" rel="noreferrer">Acquitter cet épisode</a><br><span style="overflow-wrap: anywhere;">${escapeHtml(ackUrl)}</span></li>
</ul>
<p>Le lien d’acquittement est signé et propre à cet épisode.</p>
<hr>
<p style="opacity: 0.8;">Message automatique du service d’alerte des pages d’ambassade.</p>`,
  });

  const text = `Alerte ambassade — épisode n° ${episode.id}

Un épisode d’alerte a été déclenché le ${openedAt} pour ${embassyName}.

Règles déclenchées
${rulesText(rules)}

Extrait du contenu modifié
${excerpt.trim() === '' ? 'Aucun extrait n’a été enregistré.' : excerpt}

Liens
Page de l’ambassade : ${embassyUrl ?? 'aucune URL valide enregistrée'}
Acquitter cet épisode : ${ackUrl}

Le lien d’acquittement est signé et propre à cet épisode.

Message automatique du service d’alerte des pages d’ambassade.
`;

  return {
    subject: subject(`Alerte ambassade — ${embassyName} (épisode n° ${episode.id})`),
    html,
    text,
  };
}

/**
 * Compose a separate French configuration-test email. It deliberately makes no
 * deliverability, opening or click claim; only the provider response recorded
 * by the delivery path describes whether the send was accepted or refused.
 */
export function composeTestEmail() {
  const html = documentHtml({
    title: 'Test de configuration — Alertes ambassade',
    body: `<h1 style="font-size: 1.5rem; margin-top: 0;">Test de configuration</h1>
<p>Ce message de test a été demandé depuis le service d’alerte des pages d’ambassade.</p>
<p>Il vérifie l’expéditeur, le destinataire et la clé du fournisseur enregistrés dans le service. Aucune action n’est requise.</p>
<hr>
<p style="opacity: 0.8;">Message automatique du service d’alerte des pages d’ambassade.</p>`,
  });

  const text = `Test de configuration — Alertes ambassade

Ce message de test a été demandé depuis le service d’alerte des pages d’ambassade.
Il vérifie l’expéditeur, le destinataire et la clé du fournisseur enregistrés dans le service. Aucune action n’est requise.

Message automatique du service d’alerte des pages d’ambassade.
`;

  return {
    subject: subject('Test de configuration — Alertes ambassade'),
    html,
    text,
  };
}
