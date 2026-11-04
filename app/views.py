"""Server-rendered HTML views.

Only standard-library string templates are used. Every dynamic value goes
through :func:`escape`; no user, episode or configuration value is ever
interpolated raw into markup.
"""

from __future__ import annotations

from html import escape as _escape
from typing import Any, Iterable

from . import __version__
from .config import Config
from .repositories import DeliveryAttempt, Episode


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
<header><h1>{escape(service_name)}</h1></header>
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
) -> str:
    status_text = "Service opérationnel" if database_ok else "Service dégradé"
    status_class = "" if database_ok else " bad"
    database_text = "Accessible" if database_ok else "Indisponible"
    database_class = "ok" if database_ok else "bad"
    email_text = "Configuré" if config.email_configured else "Non configuré"
    email_class = "ok" if config.email_configured else ""

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
<div class="banner{status_class}"><strong>{escape(status_text)}</strong></div>
<div class="cards">
  <div class="card"><h2>Base de données</h2><p class="{database_class}">{escape(database_text)}</p></div>
  <div class="card"><h2>Canal email</h2><p class="{escape(email_class)}">{escape(email_text)}</p></div>
  <div class="card"><h2>Épisodes</h2><p>{episode_count}</p></div>
  <div class="card"><h2>Tentatives de livraison</h2><p>{attempt_count}</p></div>
</div>
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
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Nature du changement</th><th>Page</th>"
        "<th>Détecté le</th><th>Acquittement</th></tr></thead>"
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


def error_page(*, status: int, message: str, service_name: str) -> str:
    body = f"""
<div class="banner bad"><strong>Erreur {escape(status)}</strong></div>
<p>{escape(message)}</p>
<p><a href="/">Retour à l'état du service</a></p>
"""
    return layout(
        title=f"Erreur {status}", body=body, service_name=service_name
    )
