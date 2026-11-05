import assert from 'node:assert/strict';
import test from 'node:test';
import { composeAlertEmail, composeTestEmail } from '../src/email/compose.js';

const NOW = '2026-11-04T10:00:00.000Z';
const ACK_URL = 'https://alertes.example.test/ack/header.signature';

// Wording that would assert a deliverability, opening or click measurement.
const FORBIDDEN_CLAIM =
  /(délivr|livraison|livré|ouvert|ouverture|clic|click|tracking|suivi|accusé de lecture|read receipt)/i;

function episode(overrides = {}) {
  return {
    id: 7,
    openedAt: NOW,
    embassyName: 'Ambassade de France',
    embassyPageUrl: 'https://example.test/ambassade',
    excerpt: 'Le contenu de la page a changé.',
    rules: [
      { ruleKey: 'visa', label: 'Section visa modifiée', detail: 'texte ajouté' },
      { ruleKey: 'contact', label: 'Contact modifié', detail: null },
    ],
    ...overrides,
  };
}

test('the alert message contains every rule, the excerpt and both links in French', () => {
  const message = composeAlertEmail({ episode: episode(), acknowledgementUrl: ACK_URL });

  assert.match(message.subject, /Alerte ambassade/);
  assert.match(message.subject, /Ambassade de France/);
  assert.match(message.subject, /épisode n° 7/);
  assert.equal(message.subject.includes('\n'), false);

  for (const part of [message.html, message.text]) {
    assert.match(part, /Section visa modifiée/);
    assert.match(part, /texte ajouté/);
    assert.match(part, /Contact modifié/);
    assert.match(part, /Le contenu de la page a changé/);
    assert.ok(part.includes('https://example.test/ambassade'));
    assert.ok(part.includes(ACK_URL));
    assert.match(part, /2026-11-04 10:00 UTC/);
  }
  assert.match(message.html, /<a href="https:\/\/alertes\.example\.test\/ack\/header\.signature"/);
});

test('all interpolated values are HTML-escaped in the alert message', () => {
  const message = composeAlertEmail({
    episode: episode({
      embassyName: '<b>Ambassade</b>',
      excerpt: '<script>alert("x")</script>',
      rules: [{ ruleKey: 'xss', label: '<img src=x onerror=alert(1)>', detail: '& " \'' }],
    }),
    acknowledgementUrl: ACK_URL,
  });

  assert.doesNotMatch(message.html, /<script>alert/);
  assert.doesNotMatch(message.html, /<img src=x/);
  assert.doesNotMatch(message.html, /<b>Ambassade<\/b>/);
  assert.match(message.html, /&lt;script&gt;alert\(&quot;x&quot;\)&lt;\/script&gt;/);
  assert.match(message.html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.match(message.html, /&amp; &quot; &#39;/);
  assert.match(message.text, /<script>alert\("x"\)<\/script>/);
});

test('the subject cannot be broken with header newlines', () => {
  const message = composeAlertEmail({
    episode: episode({ embassyName: 'Ambassade\r\nBcc: evil@example.test' }),
    acknowledgementUrl: ACK_URL,
  });
  assert.doesNotMatch(message.subject, /[\r\n]/);
});

test('the acknowledgement URL must be an absolute http(s) URL', () => {
  const input = episode();
  for (const acknowledgementUrl of [
    undefined,
    '',
    '/ack/header.signature',
    'javascript:alert(1)',
    'ftp://example.test/ack',
    'https://user:pass@example.test/ack',
  ]) {
    assert.throws(
      () => composeAlertEmail({ episode: input, acknowledgementUrl }),
      TypeError,
    );
  }
});

test('an invalid embassy URL is never turned into a link', () => {
  const message = composeAlertEmail({
    episode: episode({ embassyPageUrl: 'javascript:alert(1)' }),
    acknowledgementUrl: ACK_URL,
  });

  assert.doesNotMatch(message.html, /href="javascript:/);
  assert.match(message.html, /Aucune URL valide enregistrée/);
  assert.match(message.text, /aucune URL valide enregistrée/);
});

test('an episode without rules or excerpt still produces a complete French message', () => {
  const message = composeAlertEmail({
    episode: episode({ excerpt: '', rules: [] }),
    acknowledgementUrl: ACK_URL,
  });

  assert.match(message.html, /Aucune règle détaillée/);
  assert.match(message.html, /Aucun extrait n’a été enregistré/);
  assert.match(message.text, /Aucune règle détaillée/);
  assert.match(message.text, /Aucun extrait n’a été enregistré/);
  assert.ok(message.text.includes(ACK_URL));
});

test('the alert email makes no deliverability, opening or click claim', () => {
  const message = composeAlertEmail({ episode: episode(), acknowledgementUrl: ACK_URL });
  const combined = `${message.subject}\n${message.html}\n${message.text}`;
  assert.doesNotMatch(combined, FORBIDDEN_CLAIM);
});

test('the configuration-test email is French and makes no unsupported claim', () => {
  const message = composeTestEmail();
  const combined = `${message.subject}\n${message.html}\n${message.text}`;

  assert.match(message.subject, /Test de configuration/);
  assert.match(message.html, /Test de configuration/);
  assert.match(message.text, /expéditeur, le destinataire et la clé du fournisseur/);
  assert.match(message.text, /Aucune action n’est requise/);
  assert.doesNotMatch(combined, FORBIDDEN_CLAIM);
});
