"""Server-rendered HTML views.

Only standard-library string templates are used. Every dynamic value goes
through :func:`escape`; no user, episode or configuration value is ever
interpolated raw into markup.
"""

from __future__ import annotations

from html import escape as _escape
from typing import Any, Iterable
from urllib.parse import quote

from . import __version__
from .config import Config
from .repositories import DeliveryAttempt, Episode
from .sms_channel import (
    SMS_STATE_LABELS,
    SMS_STATE_UNCONFIGURED,
    SMS_STATE_VERIFIED,
    SmsChannelStatus,
)


def escape(value: Any) -> str:
    return _escape(str(value), quote=True)


def layout(*, title: str, body: str, service_name: str) -> str:
    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} — {escape(service_name)}</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif; margin: 0;
       background: #f6f7f9; color: #1c2430; }}
header {{ background: #12263f; color: #fff; padding: 1rem 1.5rem; }}
header h1 {{ margin: 0; font-size: 1.25rem; }}
header nav {{ margin-top: .5rem; display: flex; gap: 1rem; flex-wrap: wrap; }}
header nav a {{ color: #cfe0f5; font-size: .9rem; }}
header nav a:hover {{ color: #fff; }}
main {{ max-width: 1100px; margin: 0 auto; padding: 1.5rem; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
          gap: 1rem; margin-bottom: 1.5rem; }}
.card {{ background: #fff; border: 1px solid #dbe1e8; border-radius: 8px;
         padding: 1rem; }}
.card h2 {{ margin: 0 0 .35rem; font-size: .8rem; text-transform: uppercase;
            letter-spacing: .04em; color: #5a6b7f; }}
.card p {{ margin: 0; font-size: 1.05rem; font-weight: 600; }}
.ok {{ color: #146c43; }}
.bad {{ color: #b02a37; }}
.banner {{ border-radius: 8px; padding: 1rem; margin-bottom: 1.5rem;
           background: #e7f4ec; border: 1px solid #b7dfc5; }}
.banner.bad {{ background: #fbeaec; border-color: #f1b9c0; }}
table {{ width: 100%; border-collapse: collapse; background: #fff;
         border: 1px solid #dbe1e8; border-radius: 8px; overflow: hidden; }}
th, td {{ text-align: left; padding: .55rem .7rem; border-bottom: 1px solid #e7ebf0;
          font-size: .9rem; vertical-align: top; }}
th {{ background: #eef2f6; font-size: .78rem; text-transform: uppercase;
      letter-spacing: .03em; color: #4d5d70; }}
tr:last-child td {{ border-bottom: 0; }}
code {{ font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: .85em;
        word-break: break-all; }}
section {{ margin-bottom: 1.75rem; }}
section > h2 {{ font-size: 1.05rem; margin: 0 0 .6rem; }}
.empty {{ color: #66788d; font-style: italic; }}
footer {{ max-width: 1100px; margin: 0 auto; padding: 0 1.5rem 2rem;
          color: #66788d; font-size: .8rem; }}
</style>
</head>
<body>
<header><h1>{escape(service_name)}</h1>
<nav>
  <a href="/">Tableau de bord</a>
  <a href="/episodes/new">Nouvel épisode</a>
  <a href="/deliveries">Journal des livraisons</a>
</nav>
</header>
<main>
{body}
</main>
<footer>Version {escape(__version__)} — service d'alertes d'épisodes.</footer>
</body>
</html>
"""


def health_page(
    *,
    config: Config,
    database_ok: bool,
    episode_count: int,
    attempt_count: int,
    episodes: Iterable[Episode],
    attempts: Iterable[DeliveryAttempt],
    sms_status: SmsChannelStatus | None = None,
    csrf_token: str = "",
    notice: str = "",
) -> str:
    status_text = "Service opérationnel" if database_ok else "Service dégradé"
    status_class = "" if database_ok else " bad"
    database_text = "Accessible" if database_ok else "Indisponible"
    database_class = "ok" if database_ok else "bad"
    if config.email_configured:
        email_text = f"Configuré ({config.email.provider})"
        email_class = "ok"
    else:
        email_text = "Non configuré"
        email_class = "bad"

    sms_card, sms_section = _sms_view(
        sms_status=sms_status, csrf_token=csrf_token
    )

    notice_banner = ""
    notice_data = _SMS_NOTICES.get(notice)
    if notice_data:
        text, css_class = notice_data
        notice_banner = (
            f'<div class="banner{css_class}"><strong>{escape(text)}</strong></div>'
        )

    episode_rows = list(episodes)
    attempt_rows = list(attempts)

    if episode_rows:
        episode_table = _episode_table(episode_rows)
    else:
        episode_table = '<p class="empty">Aucun épisode enregistré.</p>'

    if attempt_rows:
        attempt_table = _attempt_table(attempt_rows)
    else:
        attempt_table = '<p class="empty">Aucune tentative de livraison enregistrée.</p>'

    body = f"""
{notice_banner}
<div class="banner{status_class}"><strong>{escape(status_text)}</strong></div>
<div class="cards">
  <div class="card"><h2>Base de données</h2><p class="{database_class}">{escape(database_text)}</p></div>
  <div class="card"><h2>Canal email</h2><p class="{escape(email_class)}">{escape(email_text)}</p></div>
  {sms_card}
  <div class="card"><h2>Épisodes</h2><p>{episode_count}</p></div>
  <div class="card"><h2>Tentatives de livraison</h2><p>{attempt_count}</p></div>
</div>
{sms_section}
<section>
  <h2>Derniers épisodes</h2>
  {episode_table}
</section>
<section>
  <h2>Dernières tentatives de livraison</h2>
  {attempt_table}
</section>
"""
    return layout(
        title="État du service", body=body, service_name=config.service_name
    )


def _sms_view(
    *, sms_status: SmsChannelStatus | None, csrf_token: str
) -> tuple[str, str]:
    """Render the SMS status card and its explicit test action."""

    if sms_status is None:
        label = SMS_STATE_LABELS[SMS_STATE_UNCONFIGURED]
        label_class = "bad"
        provider = "—"
        recipient = "—"
        last_test = "Aucun test effectué"
        last_error = "—"
        configuration = ""
    else:
        label = sms_status.label
        label_class = "ok" if sms_status.state == SMS_STATE_VERIFIED else "bad"
        provider = sms_status.provider or "—"
        recipient = sms_status.recipient_masked or "—"
        last_test = _sms_last_test(sms_status)
        last_error = _sms_last_error(sms_status)
        configuration = (
            f'<p class="empty">{escape(sms_status.configuration_error)}</p>'
            if sms_status.configuration_error
            else ""
        )

    card = (
        '<div class="card"><h2>Canal SMS</h2>'
        f'<p class="{label_class}">{escape(label)}</p></div>'
    )
    section = f"""
<section>
  <h2>Canal SMS</h2>
  <div class="card">
    <h2>État du canal SMS</h2>
    <p class="{label_class}">{escape(label)}</p>
    <p>Fournisseur : <code>{escape(provider)}</code></p>
    <p>Destinataire : <code>{escape(recipient)}</code></p>
    <p>Dernier test : {escape(last_test)}</p>
    <p>Dernière erreur : {escape(last_error)}</p>
    {configuration}
  </div>
  <p>Le canal SMS est facultatif. Un test réussi vérifie la configuration et
     active l'envoi lors des prochains épisodes ; le canal email reste
     obligatoire et n'est jamais dégradé par le SMS.</p>
  <form method="post" action="/sms/test">
    <input type="hidden" name="csrf_token" value="{escape(csrf_token)}">
    <button type="submit">Envoyer un SMS de test</button>
  </form>
</section>
"""
    return card, section


def _sms_last_test(status: SmsChannelStatus) -> str:
    if not status.last_test_status:
        return "Aucun test effectué"
    outcome = _SMS_TEST_RESULTS.get(
        status.last_test_status, status.last_test_status
    )
    if status.last_test_at:
        return f"{outcome} le {status.last_test_at}"
    return outcome


def _sms_last_error(status: SmsChannelStatus) -> str:
    if not status.last_error:
        return "—"
    if status.last_error_at:
        return f"{status.last_error} ({status.last_error_at})"
    return status.last_error


def _episode_table(episodes: Iterable[Episode]) -> str:
    rows = []
    for episode in episodes:
        acknowledged = episode.acknowledged_at or "En attente"
        rows.append(
            "<tr>"
            f"<td>{escape(episode.change_nature)}</td>"
            f"<td><code>{escape(episode.page_url)}</code></td>"
            f"<td>{escape(episode.detected_at)}</td>"
            f"<td>{escape(acknowledged)}</td>"
            f'<td><a href="/episodes/{escape(quote(episode.id, safe=""))}">Détail</a></td>'
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Nature du changement</th><th>Page</th>"
        "<th>Détecté le</th><th>Acquittement</th><th>Détail</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _attempt_table(attempts: Iterable[DeliveryAttempt]) -> str:
    rows = []
    for attempt in attempts:
        segments = (
            str(attempt.segment_count)
            if attempt.segment_count is not None
            else "—"
        )
        error = attempt.error or "—"
        status_class = "ok" if attempt.status == "sent" else "bad"
        rows.append(
            "<tr>"
            f"<td>{escape(attempt.created_at)}</td>"
            f"<td>{escape(attempt.channel)}</td>"
            f"<td>{escape(attempt.kind)}</td>"
            f'<td class="{status_class}">{escape(attempt.status)}</td>'
            f"<td>{escape(attempt.provider or '—')}</td>"
            f"<td>{escape(attempt.recipient_masked or '—')}</td>"
            f"<td>{escape(segments)}</td>"
            f"<td>{escape(error)}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Date</th><th>Canal</th><th>Type</th>"
        "<th>Statut</th><th>Fournisseur</th><th>Destinataire</th>"
        "<th>Segments</th><th>Erreur</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def new_episode_page(
    *,
    config: Config,
    csrf_token: str,
    error: str = "",
    values: dict[str, str] | None = None,
) -> str:
    values = values or {}
    error_banner = (
        f'<div class="banner bad"><strong>{escape(error)}</strong></div>'
        if error
        else ""
    )
    body = f"""
{error_banner}
<section>
  <h2>Créer un épisode</h2>
  <p>La création enregistre l'épisode puis tente immédiatement l'envoi de
     l'alerte email obligatoire.</p>
  <form method="post" action="/episodes">
    <input type="hidden" name="csrf_token" value="{escape(csrf_token)}">
    <p><label>Nature du changement<br>
      <input name="change_nature" required maxlength="200" size="60"
             value="{escape(values.get('change_nature', ''))}"></label></p>
    <p><label>Page concernée (URL absolue)<br>
      <input name="page_url" type="url" required maxlength="2048" size="60"
             value="{escape(values.get('page_url', ''))}"></label></p>
    <p><label>Résumé (facultatif)<br>
      <textarea name="summary" maxlength="2000" rows="4" cols="60">{escape(values.get('summary', ''))}</textarea></label></p>
    <p><button type="submit">Créer et envoyer l'alerte email</button></p>
  </form>
</section>
"""
    return layout(
        title="Nouvel épisode", body=body, service_name=config.service_name
    )


def episode_page(
    *,
    config: Config,
    episode: Episode,
    attempts: Iterable[DeliveryAttempt],
    csrf_token: str,
    notice: str = "",
) -> str:
    attempt_rows = list(attempts)
    if attempt_rows:
        attempt_table = _attempt_table(attempt_rows)
    else:
        attempt_table = '<p class="empty">Aucune tentative de livraison.</p>'

    acknowledged = episode.acknowledged_at
    if acknowledged:
        acknowledgement = (
            f'<p class="ok">Acquitté le {escape(acknowledged)}.</p>'
        )
    else:
        acknowledgement = f"""
<form method="post" action="/episodes/{escape(quote(episode.id, safe=''))}/acknowledge" id="acquitter">
  <input type="hidden" name="csrf_token" value="{escape(csrf_token)}">
  <button type="submit">Acquitter l'épisode</button>
</form>
"""

    if config.email_configured:
        email_text = f"Configuré ({config.email.provider})"
        email_class = "ok"
    else:
        email_text = "Non configuré"
        email_class = "bad"

    notice_banner = ""
    notice_data = _NOTICES.get(notice)
    if notice_data:
        text, css_class = notice_data
        notice_banner = (
            f'<div class="banner{css_class}"><strong>{escape(text)}</strong></div>'
        )

    summary = (
        f"<p><strong>Résumé :</strong> {escape(episode.summary)}</p>"
        if episode.summary
        else ""
    )

    body = f"""
{notice_banner}
<section>
  <h2>Épisode</h2>
  <p><a href="/">Retour au tableau de bord</a></p>
  <p><strong>Nature du changement :</strong> {escape(episode.change_nature)}</p>
  <p><strong>Page :</strong> <code>{escape(episode.page_url)}</code></p>
  <p><strong>Détecté le :</strong> {escape(episode.detected_at)}</p>
  {summary}
  <p><strong>Canal email :</strong> <span class="{escape(email_class)}">{escape(email_text)}</span></p>
  <p><strong>Acquittement :</strong></p>
  {acknowledgement}
</section>
<section>
  <h2>Réémission</h2>
  <p>La réémission utilise uniquement le canal email.</p>
  <form method="post" action="/episodes/{escape(quote(episode.id, safe=''))}/resend">
    <input type="hidden" name="csrf_token" value="{escape(csrf_token)}">
    <button type="submit">Réémettre l'email</button>
  </form>
</section>
<section>
  <h2>Tentatives de livraison de cet épisode</h2>
  {attempt_table}
</section>
"""
    return layout(
        title=f"Épisode — {episode.change_nature}",
        body=body,
        service_name=config.service_name,
    )


def deliveries_page(
    *, config: Config, attempts: Iterable[DeliveryAttempt]
) -> str:
    attempt_rows = list(attempts)
    if attempt_rows:
        attempt_table = _attempt_table(attempt_rows)
    else:
        attempt_table = '<p class="empty">Aucune tentative de livraison enregistrée.</p>'
    body = f"""
<section>
  <h2>Journal des livraisons</h2>
  <p>Les destinataires sont masqués et les erreurs sont expurgées.</p>
  {attempt_table}
</section>
"""
    return layout(
        title="Journal des livraisons",
        body=body,
        service_name=config.service_name,
    )


_NOTICES: dict[str, tuple[str, str]] = {
    "created": ("Épisode créé. L'alerte email a été traitée.", ""),
    "acknowledged": ("Épisode acquitté.", ""),
    "resent_sent": ("Email réémis avec succès.", ""),
    "resent_failed": (
        "La réémission email a échoué ; l'épisode reste consultable et "
        "peut être réémis à nouveau.",
        " bad",
    ),
}

_SMS_TEST_RESULTS: dict[str, str] = {
    "sent": "Réussi",
    "failed": "Échec",
}

_SMS_NOTICES: dict[str, tuple[str, str]] = {
    "sms_test_sent": (
        "Test SMS réussi : la configuration du fournisseur est vérifiée et "
        "le canal est actif.",
        "",
    ),
    "sms_test_failed": (
        "Test SMS échoué : la configuration reste inactive ou en échec. "
        "Le détail est affiché dans l'état du canal SMS.",
        " bad",
    ),
    "sms_test_refused": (
        "Test SMS refusé : le canal n'est pas configuré ou sa configuration "
        "est invalide. Aucun message n'a été envoyé.",
        " bad",
    ),
}


def error_page(*, status: int, message: str, service_name: str) -> str:
    body = f"""
<div class="banner bad"><strong>Erreur {escape(status)}</strong></div>
<p>{escape(message)}</p>
<p><a href="/">Retour à l'état du service</a></p>
"""
    return layout(
        title=f"Erreur {status}", body=body, service_name=service_name
    )
