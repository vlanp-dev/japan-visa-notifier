"""French operator dashboard and check-history pages.

These views are read-only.  They summarise the service state, the frozen
reference, the latest check with every rule result, the recent check history and
the refreeze history.  Only metadata and bounded excerpts are rendered: the
captured page itself is never injected as HTML and never written to logs.

A database failure while loading the dashboard is reported as a degraded state
instead of a generic error page, so the operator can distinguish an
infrastructure problem from a content change.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Sequence

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
from .canonical import DEFAULT_EXCERPT_LIMIT, single_line_excerpt
from .engine import (
    STATUS_DISABLED,
    STATUS_NOT_EVALUATED,
    STATUS_NOT_TRIGGERED,
    STATUS_TRIGGERED,
    RuleResult,
)
from .rules import (
    RULE_KIND_CLOSURE_BLOCK,
    RULE_KIND_NEW_ELEMENT,
    RULE_KIND_UPDATE_TIMESTAMP,
)
from .service import (
    OUTCOME_CHANGED,
    OUTCOME_CONTROL_FAILURE,
    OUTCOME_UNCHANGED,
    REASON_REFERENCE_INITIALIZED,
)
from .store import (
    CheckRecord,
    ReferenceRecord,
    RefreezeEventRecord,
    Store,
)

_CHECK_ID_PATTERN = re.compile(r"^[0-9]+$")
_HISTORY_LIMIT = 10
_REFREEZE_LIMIT = 10

_NOTICES: dict[str, str] = {
    "refrozen": (
        "La référence a été refigée. La comparaison repart de ce nouveau "
        "contenu ; aucun épisode n'a été acquitté."
    ),
    "cancelled": (
        "Le refigement a été annulé : la référence n'a pas été modifiée."
    ),
}

_OUTCOME_LABELS: dict[str, str] = {
    OUTCOME_UNCHANGED: "Aucun changement",
    OUTCOME_CHANGED: "Changement détecté",
    OUTCOME_CONTROL_FAILURE: "Échec du contrôle",
}

_STATUS_LABELS: dict[str, str] = {
    STATUS_TRIGGERED: "Déclenchée",
    STATUS_NOT_TRIGGERED: "Non déclenchée",
    STATUS_DISABLED: "Désactivée",
    STATUS_NOT_EVALUATED: "Non évaluée",
}

_KIND_LABELS: dict[str, str] = {
    RULE_KIND_CLOSURE_BLOCK: "Bloc d'annonce de fermeture",
    RULE_KIND_UPDATE_TIMESTAMP: "Horodatage de mise à jour",
    RULE_KIND_NEW_ELEMENT: "Nouvel élément absent de la référence",
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


@dataclass(frozen=True)
class _DashboardData:
    reference: ReferenceRecord | None
    latest: CheckRecord | None
    results: tuple[RuleResult, ...]
    checks: tuple[CheckRecord, ...]
    refreezes: tuple[RefreezeEventRecord, ...]


def register_dashboard_routes(
    application: Application,
    store: Store,
    config: Config,
    logger: StructuredLogger | None = None,
) -> None:
    """Register the read-only dashboard and check-detail routes."""

    application.add_route(
        "GET", r"/", _dashboard_handler(config, store, logger)
    )
    # Only numeric ids reach the custom handler; anything else is a normal
    # page-not-found response.
    application.add_route(
        "GET",
        r"/checks/(?P<check_id>[0-9]+)",
        _check_detail_handler(config, store, logger),
    )


# -- handlers ---------------------------------------------------------


def _dashboard_handler(
    config: Config,
    store: Store,
    logger: StructuredLogger | None,
):
    def handle(request: Request) -> Response:
        notice = _NOTICES.get(request.query_one("ok") or "")
        try:
            data = _load_dashboard(store)
        except sqlite3.Error as exc:
            _log(logger, "dashboard_unavailable", error_type=type(exc).__name__)
            return html_response(
                200,
                render_page(
                    "Veille du calendrier",
                    _degraded_content(config, notice),
                ),
            )
        return html_response(
            200,
            render_page(
                "Veille du calendrier",
                _dashboard_content(config, data, notice),
            ),
        )

    return handle


def _check_detail_handler(
    config: Config,
    store: Store,
    logger: StructuredLogger | None,
):
    def handle(request: Request, check_id: str) -> Response:
        parsed = _parse_check_id(check_id)
        if parsed is None:
            return _not_found()
        try:
            check = store.get_check(parsed)
            if check is None:
                return _not_found()
            results = store.results_for_check(check.id)
        except sqlite3.Error as exc:
            _log(logger, "check_detail_unavailable", error_type=type(exc).__name__)
            return html_response(
                200,
                render_page(
                    "Détail du contrôle",
                    _degraded_content(config, None),
                ),
            )
        content = _check_detail_content(check, results)
        return html_response(200, render_page("Détail du contrôle", content))

    return handle


def _load_dashboard(store: Store) -> _DashboardData:
    # Touching every table behind the health indicator means a missing schema
    # is reported as degraded rather than as an incomplete dashboard.
    store.list_rules()
    reference = store.current_reference()
    latest = store.latest_check()
    results = (
        tuple(store.results_for_check(latest.id)) if latest is not None else ()
    )
    checks = tuple(store.list_checks(limit=_HISTORY_LIMIT))
    refreezes = tuple(store.list_refreeze_events(limit=_REFREEZE_LIMIT))
    return _DashboardData(
        reference=reference,
        latest=latest,
        results=results,
        checks=checks,
        refreezes=refreezes,
    )


def _parse_check_id(raw: str) -> int | None:
    if not _CHECK_ID_PATTERN.fullmatch(raw or ""):
        return None
    value = int(raw, 10)
    return value if value >= 1 else None


# -- rendering: dashboard ---------------------------------------------


def _dashboard_content(
    config: Config,
    data: _DashboardData,
    notice: str | None,
) -> str:
    parts = [
        "<h1>Veille du calendrier</h1>",
        "<p>"
        '<a href="/rules">Gérer les règles de détection</a> · '
        '<a href="/reference/refreeze">Refiger la référence</a>'
        "</p>",
    ]
    if notice:
        parts.append(f'<p role="status">{escape(notice)}</p>')

    parts.append("<h2>État du service</h2>")
    parts.append(
        "<dl>"
        "<dt>État</dt><dd>opérationnel</dd>"
        f"<dt>Page surveillée</dt><dd>{escape(config.target_url)}</dd>"
        "<dt>Intervalle de contrôle</dt>"
        f"<dd>{escape(config.interval_seconds)} secondes</dd>"
        f"<dt>Opérateur</dt><dd>{escape(config.operator_label)}</dd>"
        "</dl>"
    )

    parts.append("<h2>Référence figée</h2>")
    parts.extend(_reference_content(data.reference))

    parts.append("<h2>Dernier contrôle</h2>")
    if data.latest is None:
        parts.append("<p>Aucun contrôle n'a encore été exécuté.</p>")
    else:
        parts.extend(_check_summary(data.latest))
        parts.append("<h3>Résultat par règle</h3>")
        parts.extend(_results_content(data.results))

    parts.append("<h2>Historique récent des contrôles</h2>")
    parts.extend(_history_content(data.checks))

    parts.append("<h2>Refigements</h2>")
    parts.extend(_refreeze_content(data.refreezes))
    return "\n".join(parts)


def _reference_content(reference: ReferenceRecord | None) -> list[str]:
    if reference is None:
        return [
            "<p>Aucune référence n'est encore figée. Le premier contrôle "
            "réussi la créera automatiquement.</p>"
        ]
    return [
        "<dl>"
        f"<dt>Identifiant</dt><dd>{escape(reference.id)}</dd>"
        "<dt>Empreinte SHA-256</dt>"
        f"<dd><code>{escape(reference.content_hash)}</code></dd>"
        "<dt>Type de contenu</dt>"
        f"<dd>{escape(reference.content_type or '—')}</dd>"
        f"<dt>Capturée le</dt><dd>{escape(reference.captured_at)}</dd>"
        f"<dt>Opérateur</dt><dd>{escape(reference.operator_label)}</dd>"
        "</dl>"
    ]


def _check_summary(check: CheckRecord) -> list[str]:
    outcome_label = _OUTCOME_LABELS.get(check.outcome, check.outcome)
    parts = [f"<p><strong>{escape(outcome_label)}</strong></p>"]

    if check.outcome == OUTCOME_UNCHANGED and check.reason:
        if check.reason == REASON_REFERENCE_INITIALIZED:
            parts.append(
                "<p>Référence initiale capturée "
                "(<code>reference_initialized</code>).</p>"
            )
        else:
            parts.append(
                f"<p>Motif : <code>{escape(check.reason)}</code></p>"
            )

    if check.outcome == OUTCOME_CONTROL_FAILURE:
        category = check.failure_category or "inconnue"
        parts.append(
            "<p>Catégorie d'échec : "
            f"<code>{escape(category)}</code>"
            f" ({escape(_FAILURE_LABELS.get(category, 'échec de contrôle'))})"
            "</p>"
        )
        if check.failure_message:
            parts.append(
                f"<p>Message : {escape(check.failure_message)}</p>"
            )

    parts.append("<dl>")
    parts.append(f"<dt>Début</dt><dd>{escape(check.started_at)}</dd>")
    if check.finished_at:
        parts.append(
            f"<dt>Fin</dt><dd>{escape(check.finished_at)}</dd>"
        )
    if check.duration_ms is not None:
        parts.append(
            f"<dt>Durée</dt><dd>{escape(check.duration_ms)} ms</dd>"
        )
    if check.http_status is not None:
        parts.append(
            f"<dt>Statut HTTP</dt><dd>{escape(check.http_status)}</dd>"
        )
    parts.append("</dl>")
    return parts


def _results_content(results: Sequence[RuleResult]) -> list[str]:
    if not results:
        return ["<p>Aucun résultat par règle n'est enregistré pour ce contrôle.</p>"]

    parts = [
        "<table>",
        "<thead><tr><th>Règle</th><th>Type</th><th>Statut</th>"
        "<th>Extrait actuel</th><th>Extrait de référence</th>"
        "<th>Détail</th></tr></thead>",
        "<tbody>",
    ]
    for result in results:
        kind_label = _KIND_LABELS.get(result.kind, result.kind)
        status_label = _STATUS_LABELS.get(result.status, result.status)
        parts.append(
            "<tr>"
            f"<td>{escape(result.rule_name)}<br><small>Identifiant : "
            f"{escape(result.rule_id)}</small></td>"
            f"<td>{escape(kind_label)}<br><small>"
            f"{escape(result.kind)}</small></td>"
            f"<td>{escape(status_label)}</td>"
            f"<td>{escape(_bounded(result.current_excerpt))}</td>"
            f"<td>{escape(_bounded(result.reference_excerpt))}</td>"
            f"<td>{escape(_bounded(result.detail))}</td>"
            "</tr>"
        )
    parts.append("</tbody>")
    parts.append("</table>")
    return parts


def _history_content(checks: Sequence[CheckRecord]) -> list[str]:
    if not checks:
        return ["<p>Aucun contrôle n'a encore été enregistré.</p>"]

    parts = [
        "<table>",
        "<thead><tr><th>Contrôle</th><th>Début</th><th>Résultat</th>"
        "<th>Précision</th><th></th></tr></thead>",
        "<tbody>",
    ]
    for check in checks:
        outcome_label = _OUTCOME_LABELS.get(check.outcome, check.outcome)
        parts.append(
            "<tr>"
            f"<td>{escape(check.id)}</td>"
            f"<td>{escape(check.started_at)}</td>"
            f"<td>{escape(outcome_label)}</td>"
            f"<td>{escape(_history_detail(check))}</td>"
            f'<td><a href="/checks/{escape(check.id)}">Détail</a></td>'
            "</tr>"
        )
    parts.append("</tbody>")
    parts.append("</table>")
    return parts


def _history_detail(check: CheckRecord) -> str:
    if check.outcome == OUTCOME_CONTROL_FAILURE:
        category = check.failure_category or "inconnue"
        return f"{category} : {_FAILURE_LABELS.get(category, 'échec de contrôle')}"
    if check.reason:
        return check.reason
    return ""


def _refreeze_content(events: Sequence[RefreezeEventRecord]) -> list[str]:
    if not events:
        return ["<p>Aucun refigement n'a encore été enregistré.</p>"]

    parts = [
        "<table>",
        "<thead><tr><th>Date</th><th>Résultat</th>"
        "<th>Référence précédente</th><th>Nouvelle référence</th>"
        "<th>Opérateur</th><th>Message</th></tr></thead>",
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


# -- rendering: check detail ------------------------------------------


def _check_detail_content(
    check: CheckRecord,
    results: Sequence[RuleResult],
) -> str:
    parts = [
        "<h1>Détail du contrôle</h1>",
        '<p><a href="/">Retour au tableau de bord</a></p>',
    ]
    parts.extend(_check_summary(check))
    parts.append("<h2>Résultat par règle</h2>")
    parts.extend(_results_content(results))
    parts.append('<p><a href="/">Retour au tableau de bord</a></p>')
    return "\n".join(parts)


# -- rendering: degraded state ----------------------------------------


def _degraded_content(config: Config, notice: str | None) -> str:
    parts = [
        "<h1>Veille du calendrier</h1>",
        "<p>"
        '<a href="/rules">Gérer les règles de détection</a> · '
        '<a href="/reference/refreeze">Refiger la référence</a>'
        "</p>",
    ]
    if notice:
        parts.append(f'<p role="status">{escape(notice)}</p>')
    parts.append(
        '<div role="alert"><p><strong>État : dégradé.</strong> '
        "La base de données n'a pas pu être interrogée. Les résultats et "
        "l'historique sont momentanément indisponibles ; les contrôles "
        "reprennent dès que la base est accessible.</p></div>"
    )
    parts.append(
        "<dl>"
        f"<dt>Page surveillée</dt><dd>{escape(config.target_url)}</dd>"
        "<dt>Intervalle de contrôle</dt>"
        f"<dd>{escape(config.interval_seconds)} secondes</dd>"
        f"<dt>Opérateur</dt><dd>{escape(config.operator_label)}</dd>"
        "</dl>"
    )
    return "\n".join(parts)


# -- small helpers ----------------------------------------------------


def _bounded(value: str | None) -> str:
    return single_line_excerpt(value or "", DEFAULT_EXCERPT_LIMIT)


def _identifier(value: int | None) -> str:
    return "—" if value is None else str(value)


def _not_found() -> Response:
    return html_response(
        404,
        render_page(
            "Contrôle introuvable",
            "<h1>Contrôle introuvable</h1>"
            "<p>Ce contrôle n'existe pas ou a été supprimé.</p>"
            '<p><a href="/">Retour au tableau de bord</a></p>',
        ),
    )


def _log(
    logger: StructuredLogger | None,
    event: str,
    **fields: object,
) -> None:
    if logger is not None:
        logger.log(event, **fields)


__all__ = ["register_dashboard_routes"]
