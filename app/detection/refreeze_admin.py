"""French operator confirmation page for refreezing the reference.

The page only displays metadata: the target URL, the current reference hash,
capture time and operator, plus the refreeze history.  Page content itself is
never rendered.  The POST requires the double-submit CSRF token enforced by the
web layer and an explicit ``confirm=yes`` field; anything else is a no-op.
"""

from __future__ import annotations

from ..config import Config
from ..logging_setup import StructuredLogger
from ..web import (
    Application,
    Request,
    Response,
    escape,
    html_response,
    render_page,
)
from .refreeze import RefreezeResult, RefreezeService
from .store import ReferenceRecord, RefreezeEventRecord, Store

_NOTICES: dict[str, str] = {
    "refrozen": (
        "La référence a été refigée. La comparaison repart de ce nouveau "
        "contenu ; aucun épisode n'a été acquitté."
    ),
    "cancelled": (
        "Le refigement a été annulé : la référence n'a pas été modifiée."
    ),
}

_FAILURE_LABELS: dict[str, str] = {
    "network_error": "page inaccessible",
    "timeout": "délai de récupération dépassé",
    "http_status": "statut HTTP inattendu",
    "empty_body": "page vide",
    "unsupported_content_type": "type de contenu non pris en charge",
    "oversized_response": "réponse trop volumineuse",
    "unparseable_content": "contenu illisible",
}

_HISTORY_LIMIT = 10


def register_refreeze_routes(
    application: Application,
    store: Store,
    service: RefreezeService,
    config: Config,
    logger: StructuredLogger | None = None,
) -> None:
    """Register the GET confirmation and POST refreeze routes."""

    application.add_route(
        "GET", r"/reference/refreeze", _confirmation_handler(store, config)
    )
    application.add_route(
        "POST",
        r"/reference/refreeze",
        _refreeze_handler(store, service, config, logger),
    )


def _confirmation_handler(store: Store, config: Config):
    def handle(request: Request) -> Response:
        notice = _NOTICES.get(request.query_one("ok") or "")
        content = _page_content(
            request,
            config,
            store.current_reference(),
            store.list_refreeze_events(limit=_HISTORY_LIMIT),
            notice=notice,
        )
        return html_response(200, render_page("Refiger la référence", content))

    return handle


def _refreeze_handler(
    store: Store,
    service: RefreezeService,
    config: Config,
    logger: StructuredLogger | None,
):
    def handle(request: Request) -> Response:
        confirmed = (request.form_one("confirm") or "").strip().lower()
        if confirmed != "yes":
            _log(logger, "refreeze_cancelled")
            return _redirect("/reference/refreeze?ok=cancelled")

        result = service.refreeze()
        if not result.success:
            content = _page_content(
                request,
                config,
                store.current_reference(),
                store.list_refreeze_events(limit=_HISTORY_LIMIT),
                error=_failure_text(result),
            )
            return html_response(
                200, render_page("Refigement en échec", content)
            )

        _log(
            logger,
            "refreeze_confirmed",
            event_id=result.event.id,
            reference_id=result.reference.id if result.reference else None,
        )
        return _redirect("/reference/refreeze?ok=refrozen")

    return handle


def _failure_text(result: RefreezeResult) -> str:
    label = _FAILURE_LABELS.get(
        result.failure_category or "", "échec de récupération"
    )
    message = result.failure_message or ""
    category = result.failure_category or "inconnue"
    return (
        "Le refigement n'a pas été effectué : "
        f"{label} ({category}). {message} "
        "La référence actuelle a été conservée."
    )


# -- rendering --------------------------------------------------------


def _page_content(
    request: Request,
    config: Config,
    reference: ReferenceRecord | None,
    events: list[RefreezeEventRecord],
    *,
    notice: str | None = None,
    error: str | None = None,
) -> str:
    parts = [
        "<h1>Refiger la référence</h1>",
        '<p><a href="/">Retour au tableau de bord</a></p>',
    ]
    if notice:
        parts.append(f'<p role="status">{escape(notice)}</p>')
    if error:
        parts.append(f'<div role="alert"><p>{escape(error)}</p></div>')

    parts.append("<h2>Page cible</h2>")
    parts.append(
        "<dl>"
        f"<dt>Adresse</dt><dd>{escape(config.target_url)}</dd>"
        "</dl>"
    )

    parts.append("<h2>Référence actuelle</h2>")
    if reference is None:
        parts.append(
            "<p>Aucune référence n'est actuellement enregistrée. Un "
            "refigement réussi créera la référence initiale.</p>"
        )
    else:
        parts.append("<dl>")
        parts.append(f"<dt>Identifiant</dt><dd>{escape(reference.id)}</dd>")
        parts.append(
            "<dt>Empreinte SHA-256</dt>"
            f"<dd><code>{escape(reference.content_hash)}</code></dd>"
        )
        parts.append(
            "<dt>Type de contenu</dt>"
            f"<dd>{escape(reference.content_type or '—')}</dd>"
        )
        parts.append(
            f"<dt>Capturée le</dt><dd>{escape(reference.captured_at)}</dd>"
        )
        parts.append(
            "<dt>Opérateur</dt>"
            f"<dd>{escape(reference.operator_label)}</dd>"
        )
        parts.append("</dl>")

    parts.append(
        '<div role="alert">'
        "<p><strong>Cette action remplace la référence figée actuelle.</strong> "
        "La page cible sera récupérée une seule fois et le contenu validé "
        "deviendra la nouvelle référence. Si la récupération échoue, la "
        "référence actuelle reste utilisable et l'échec est enregistré dans "
        "l'historique.</p>"
        "</div>"
    )

    parts.append('<form method="post" action="/reference/refreeze">')
    parts.append(_csrf_input(request.csrf_token))
    parts.append('<input type="hidden" name="confirm" value="yes">')
    parts.append(
        '<p><button type="submit">Refiger la référence</button> '
        '<a href="/">Annuler</a></p>'
    )
    parts.append("</form>")

    parts.append("<h2>Historique des refigements</h2>")
    parts.extend(_history_content(events))
    return "\n".join(parts)


def _history_content(events: list[RefreezeEventRecord]) -> list[str]:
    if not events:
        return ["<p>Aucun refigement n'a encore été enregistré.</p>"]

    parts = [
        "<table>",
        "<thead><tr><th>Date</th><th>Résultat</th><th>Référence précédente</th>"
        "<th>Nouvelle référence</th><th>Opérateur</th><th>Message</th></tr></thead>",
        "<tbody>",
    ]
    for event in events:
        status = "Réussite" if event.success else "Échec"
        parts.append(
            "<tr>"
            f"<td>{escape(event.occurred_at)}</td>"
            f"<td>{escape(status)}</td>"
            f"<td>{escape(_identifier(event.previous_reference_id))}</td>"
            f"<td>{escape(_identifier(event.new_reference_id))}</td>"
            f"<td>{escape(event.operator_label)}</td>"
            f"<td>{escape(event.message or '')}</td>"
            "</tr>"
        )
    parts.append("</tbody>")
    parts.append("</table>")
    return parts


def _identifier(value: int | None) -> str:
    return "—" if value is None else str(value)


def _csrf_input(token: str | None) -> str:
    return (
        '<input type="hidden" name="csrf_token" '
        f'value="{escape(token or "")}">'
    )


def _redirect(location: str) -> Response:
    return Response(
        status=303,
        body=b"",
        headers=[
            ("Location", location),
            ("Content-Type", "text/html; charset=utf-8"),
        ],
    )


def _log(
    logger: StructuredLogger | None,
    event: str,
    **fields: object,
) -> None:
    if logger is not None:
        logger.log(event, **fields)


__all__ = ["register_refreeze_routes"]
