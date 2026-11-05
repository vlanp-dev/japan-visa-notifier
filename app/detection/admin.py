"""French operator interface for managing detection rules.

The routes below only read and write the ``rules`` table.  They never touch the
frozen reference, never run a check and never emit a change event: rule edits
take effect on the next scheduled check, exactly like a configuration change.

Every POST is protected by the double-submit CSRF middleware.  Names and
parameters are validated before any write, so invalid input leaves the store
untouched and is reported with French messages.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from typing import Any, Mapping

from ..logging_setup import StructuredLogger
from ..web import (
    Application,
    Request,
    Response,
    escape,
    html_response,
    render_page,
)
from .canonical import (
    DEFAULT_CLOSURE_ANCHOR,
    DEFAULT_TIMESTAMP_LABEL,
    DEFAULT_TIMESTAMP_WINDOW,
)
from .rules import (
    ELEMENT_SOURCES,
    MAX_RULE_NAME_LENGTH,
    MAX_TIMESTAMP_WINDOW,
    RULE_KINDS,
    RULE_KIND_CLOSURE_BLOCK,
    RULE_KIND_NEW_ELEMENT,
    RULE_KIND_UPDATE_TIMESTAMP,
    Rule,
    RuleValidationError,
    normalize_rule,
)
from .store import Store

RULE_ID_PATTERN = r"[A-Za-z0-9._-]+"

RULE_KIND_LABELS: dict[str, str] = {
    RULE_KIND_CLOSURE_BLOCK: "Bloc d'annonce de fermeture",
    RULE_KIND_UPDATE_TIMESTAMP: "Horodatage de mise à jour",
    RULE_KIND_NEW_ELEMENT: "Nouvel élément absent de la référence",
}

SOURCE_LABELS: dict[str, str] = {
    "document": "documents datés",
    "link": "liens",
    "time": "éléments datés (<time>)",
}

_NOTICES: dict[str, str] = {
    "added": "La règle a été ajoutée. Elle prendra effet au prochain contrôle.",
    "updated": "La règle a été mise à jour. Elle prendra effet au prochain contrôle.",
    "enabled": "La règle est maintenant active.",
    "disabled": "La règle est maintenant inactive.",
}

_ERRORS: dict[str, str] = {
    "name-required": "Le nom de la règle est obligatoire.",
    "name-too-long": (
        f"Le nom de la règle ne doit pas dépasser {MAX_RULE_NAME_LENGTH} caractères."
    ),
    "name-control": "Le nom de la règle contient des caractères non autorisés.",
    "kind-unsupported": "Le type de règle n'est pas pris en charge.",
    "anchor-required": "L'ancre du bloc doit contenir au moins 3 caractères.",
    "anchor-too-long": "L'ancre du bloc est trop longue (160 caractères maximum).",
    "anchor-generic": (
        "L'ancre ne peut pas être un mot générique comme « rendez-vous » "
        "ou « calendrier » : choisissez un texte propre à l'annonce de fermeture."
    ),
    "label-required": "Le libellé de l'horodatage doit contenir au moins 3 caractères.",
    "label-too-long": "Le libellé de l'horodatage est trop long (160 caractères maximum).",
    "label-generic": (
        "Le libellé ne peut pas être un mot générique comme « rendez-vous » "
        "ou « calendrier »."
    ),
    "window-invalid": (
        f"La fenêtre doit être un entier compris entre 1 et {MAX_TIMESTAMP_WINDOW}."
    ),
    "sources-empty": "Sélectionnez au moins une famille d'éléments à surveiller.",
    "sources-unsupported": (
        "Une famille d'éléments sélectionnée n'est pas prise en charge."
    ),
    "duplicate-name": "Une autre règle porte déjà ce nom.",
    "generic-parameter": "Un paramètre est invalide.",
}


def register_rule_routes(
    application: Application,
    store: Store,
    logger: StructuredLogger | None = None,
) -> None:
    """Register the French rule administration routes on ``application``."""

    application.add_route("GET", r"/rules", _list_handler(store))
    application.add_route("GET", r"/rules/new", _new_form_handler(store))
    application.add_route("POST", r"/rules", _add_handler(store, logger))
    application.add_route(
        "GET",
        rf"/rules/(?P<rule_id>{RULE_ID_PATTERN})/edit",
        _edit_form_handler(store),
    )
    application.add_route(
        "POST",
        rf"/rules/(?P<rule_id>{RULE_ID_PATTERN})",
        _update_handler(store, logger),
    )
    application.add_route(
        "POST",
        rf"/rules/(?P<rule_id>{RULE_ID_PATTERN})/enable",
        _toggle_handler(store, logger, enabled=True),
    )
    application.add_route(
        "POST",
        rf"/rules/(?P<rule_id>{RULE_ID_PATTERN})/disable",
        _toggle_handler(store, logger, enabled=False),
    )


# -- handlers ---------------------------------------------------------


def _list_handler(store: Store):
    def handle(request: Request) -> Response:
        notice = _NOTICES.get(request.query_one("ok") or "")
        content = _list_content(
            store.list_rules(),
            notice=notice,
            csrf_token=request.csrf_token,
        )
        return html_response(
            200, render_page("Règles de détection", content)
        )

    return handle


def _new_form_handler(store: Store):
    def handle(request: Request) -> Response:
        content = _form_content(
            request,
            values=_default_values(),
            errors=[],
            action="/rules",
            title="Ajouter une règle",
            submit_label="Ajouter la règle",
            kind_locked=False,
        )
        return html_response(200, render_page("Ajouter une règle", content))

    return handle


def _add_handler(store: Store, logger: StructuredLogger | None):
    def handle(request: Request) -> Response:
        rules = store.list_rules()
        values = _values_from_form(request)
        errors: list[str] = []
        rule: Rule | None = None
        try:
            rule_id = _generate_rule_id(values["name"], {r.id for r in rules})
            rule = _rule_from_values(rule_id, values)
        except RuleValidationError as exc:
            errors.extend(_translate_errors(exc.errors))

        if _duplicate_name(values["name"], rules) is not None:
            errors.append(_ERRORS["duplicate-name"])

        if errors or rule is None:
            return _invalid_form(request, values, errors, action="/rules")

        try:
            store.add_rule(rule)
        except sqlite3.IntegrityError:
            return _invalid_form(
                request,
                values,
                [_ERRORS["duplicate-name"]],
                action="/rules",
            )
        _log(logger, "rule_added", rule_id=rule.id, kind=rule.kind)
        return _redirect("/rules?ok=added")

    return handle


def _edit_form_handler(store: Store):
    def handle(request: Request, rule_id: str) -> Response:
        rule = store.get_rule(rule_id)
        if rule is None:
            return _not_found()
        content = _form_content(
            request,
            values=_values_from_rule(rule),
            errors=[],
            action=f"/rules/{rule.id}",
            title="Modifier la règle",
            submit_label="Enregistrer les modifications",
            kind_locked=True,
        )
        return html_response(200, render_page("Modifier la règle", content))

    return handle


def _update_handler(store: Store, logger: StructuredLogger | None):
    def handle(request: Request, rule_id: str) -> Response:
        existing = store.get_rule(rule_id)
        if existing is None:
            return _not_found()

        values = _values_from_form(request)
        # The kind of an existing rule is fixed: changing it would silently
        # reinterpret previously validated parameters.
        values["kind"] = existing.kind
        rules = store.list_rules()
        errors: list[str] = []
        rule: Rule | None = None
        try:
            rule = _rule_from_values(rule_id, values)
        except RuleValidationError as exc:
            errors.extend(_translate_errors(exc.errors))

        if _duplicate_name(values["name"], rules, exclude_id=rule_id) is not None:
            errors.append(_ERRORS["duplicate-name"])

        if errors or rule is None:
            return _invalid_form(
                request,
                values,
                errors,
                action=f"/rules/{rule_id}",
                kind_locked=True,
            )

        try:
            store.update_rule(rule)
        except KeyError:
            return _not_found()
        except sqlite3.IntegrityError:
            return _invalid_form(
                request,
                values,
                [_ERRORS["duplicate-name"]],
                action=f"/rules/{rule_id}",
                kind_locked=True,
            )
        _log(logger, "rule_updated", rule_id=rule.id, kind=rule.kind)
        return _redirect("/rules?ok=updated")

    return handle


def _toggle_handler(
    store: Store,
    logger: StructuredLogger | None,
    *,
    enabled: bool,
):
    def handle(request: Request, rule_id: str) -> Response:
        try:
            rule = store.set_rule_enabled(rule_id, enabled)
        except KeyError:
            return _not_found()
        _log(
            logger,
            "rule_enabled" if enabled else "rule_disabled",
            rule_id=rule.id,
            kind=rule.kind,
        )
        return _redirect(f"/rules?ok={'enabled' if enabled else 'disabled'}")

    return handle


# -- form values and validation ---------------------------------------


def _default_values() -> dict[str, Any]:
    return {
        "name": "",
        "kind": RULE_KIND_CLOSURE_BLOCK,
        "enabled": True,
        "anchor": DEFAULT_CLOSURE_ANCHOR,
        "label": DEFAULT_TIMESTAMP_LABEL,
        "window": DEFAULT_TIMESTAMP_WINDOW,
        "sources": list(ELEMENT_SOURCES),
    }


def _values_from_rule(rule: Rule) -> dict[str, Any]:
    parameters = rule.parameters
    return {
        "name": rule.name,
        "kind": rule.kind,
        "enabled": rule.enabled,
        "anchor": parameters.get("anchor", DEFAULT_CLOSURE_ANCHOR),
        "label": parameters.get("label", DEFAULT_TIMESTAMP_LABEL),
        "window": parameters.get("window", DEFAULT_TIMESTAMP_WINDOW),
        "sources": list(parameters.get("sources", ELEMENT_SOURCES)),
    }


def _values_from_form(request: Request) -> dict[str, Any]:
    defaults = _default_values()
    return {
        "name": request.form_one("name") or "",
        "kind": (request.form_one("kind") or "").strip(),
        "enabled": request.form_one("enabled") is not None,
        "anchor": _form_text(request, "anchor", defaults["anchor"]),
        "label": _form_text(request, "label", defaults["label"]),
        "window": _form_text(request, "window", defaults["window"]),
        "sources": list(request.form.get("sources") or []),
    }


def _form_text(request: Request, name: str, default: Any) -> Any:
    value = request.form_one(name)
    return default if value is None else value


def _rule_from_values(rule_id: str, values: Mapping[str, Any]) -> Rule:
    kind = values["kind"]
    if kind == RULE_KIND_CLOSURE_BLOCK:
        parameters: Mapping[str, Any] = {"anchor": values["anchor"]}
    elif kind == RULE_KIND_UPDATE_TIMESTAMP:
        parameters = {"label": values["label"], "window": values["window"]}
    elif kind == RULE_KIND_NEW_ELEMENT:
        parameters = {"sources": list(values["sources"])}
    else:
        parameters = {}

    return normalize_rule(
        Rule(
            id=rule_id,
            name=values["name"],
            kind=kind,
            enabled=bool(values["enabled"]),
            parameters=parameters,
        )
    )


def _duplicate_name(
    name: str, rules: list[Rule], *, exclude_id: str | None = None
) -> Rule | None:
    folded = name.strip().casefold()
    if not folded:
        return None
    for rule in rules:
        if rule.id == exclude_id:
            continue
        if rule.name.strip().casefold() == folded:
            return rule
    return None


def _generate_rule_id(name: str, existing: set[str]) -> str:
    base = _slugify(name)[:48].strip("-") or "regle"
    candidate = base
    suffix = 2
    while candidate in existing:
        candidate = f"{base[:44]}-{suffix}"
        suffix += 1
    return candidate


def _slugify(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name)
    without_marks = "".join(
        ch for ch in decomposed if not unicodedata.combining(ch)
    )
    return re.sub(r"[^a-z0-9]+", "-", without_marks.casefold()).strip("-")


def _translate_errors(errors: list[str]) -> list[str]:
    translated: list[str] = []
    for error in errors:
        message = _translate_error(error)
        if message not in translated:
            translated.append(message)
    return translated or [_ERRORS["generic-parameter"]]


def _translate_error(error: str) -> str:
    if error.startswith("name must not be empty"):
        return _ERRORS["name-required"]
    if error.startswith("name must be at most"):
        return _ERRORS["name-too-long"]
    if error.startswith("name must not contain") or error.startswith(
        "name must be a string"
    ):
        return _ERRORS["name-control"]
    if error.startswith("unsupported rule kind") or error.startswith(
        "kind must be"
    ):
        return _ERRORS["kind-unsupported"]
    if error.startswith("anchor must be at least") or error.startswith(
        "anchor must be a string"
    ):
        return _ERRORS["anchor-required"]
    if error.startswith("anchor must be at most"):
        return _ERRORS["anchor-too-long"]
    if error.startswith("anchor must not be a generic word"):
        return _ERRORS["anchor-generic"]
    if error.startswith("anchor must not contain"):
        return _ERRORS["anchor-required"]
    if error.startswith("label must be at least") or error.startswith(
        "label must be a string"
    ):
        return _ERRORS["label-required"]
    if error.startswith("label must be at most"):
        return _ERRORS["label-too-long"]
    if error.startswith("label must not be a generic word"):
        return _ERRORS["label-generic"]
    if error.startswith("label must not contain"):
        return _ERRORS["label-required"]
    if error.startswith("window must be"):
        return _ERRORS["window-invalid"]
    if error.startswith("sources must contain at least") or error.startswith(
        "sources must be a list"
    ):
        return _ERRORS["sources-empty"]
    if error.startswith("unsupported element source") or error.startswith(
        "sources must contain strings"
    ):
        return _ERRORS["sources-unsupported"]
    return _ERRORS["generic-parameter"]


# -- rendering --------------------------------------------------------


def _list_content(
    rules: list[Rule], *, notice: str | None, csrf_token: str | None
) -> str:
    parts = [
        "<h1>Règles de détection</h1>",
        "<p>Les règles actives sont combinées en OU : une seule règle "
        "déclenchée suffit à signaler un changement. Une modification de règle "
        "prend effet au prochain contrôle et ne modifie jamais la référence "
        "figée.</p>",
        '<p><a href="/rules/new">Ajouter une règle</a></p>',
    ]
    if notice:
        parts.append(f'<p role="status">{escape(notice)}</p>')

    if not rules:
        parts.append("<p>Aucune règle n'est enregistrée.</p>")
        return "\n".join(parts)

    parts.append("<table>")
    parts.append(
        "<thead><tr><th>Nom</th><th>Type</th><th>État</th>"
        "<th>Paramètres</th><th>Actions</th></tr></thead>"
    )
    parts.append("<tbody>")
    for rule in rules:
        enabled = "Active" if rule.enabled else "Inactive"
        kind_label = RULE_KIND_LABELS.get(rule.kind, rule.kind)
        params = _format_parameters(rule)
        toggle_action = "disable" if rule.enabled else "enable"
        toggle_label = "Désactiver" if rule.enabled else "Activer"
        parts.append(
            "<tr>"
            f"<td>{escape(rule.name)}<br><small>"
            f"Identifiant : {escape(rule.id)}</small></td>"
            f"<td>{escape(kind_label)}<br><small>{escape(rule.kind)}</small></td>"
            f"<td>{escape(enabled)}</td>"
            f"<td>{escape(params)}</td>"
            "<td>"
            f'<a href="/rules/{escape(rule.id)}/edit">Modifier</a> '
            f'<form method="post" action="/rules/{escape(rule.id)}/'
            f'{toggle_action}">'
            f"{_csrf_input(csrf_token)}"
            f'<button type="submit">{escape(toggle_label)}</button>'
            "</form>"
            "</td>"
            "</tr>"
        )
    parts.append("</tbody>")
    parts.append("</table>")
    return "\n".join(parts)


def _format_parameters(rule: Rule) -> str:
    parameters = rule.parameters
    if rule.kind == RULE_KIND_CLOSURE_BLOCK:
        return f"Ancre : « {parameters.get('anchor', '')} »"
    if rule.kind == RULE_KIND_UPDATE_TIMESTAMP:
        return (
            f"Libellé : « {parameters.get('label', '')} » ; "
            f"fenêtre : {parameters.get('window', '')} caractères"
        )
    if rule.kind == RULE_KIND_NEW_ELEMENT:
        labels = [
            SOURCE_LABELS.get(source, source)
            for source in parameters.get("sources", [])
        ]
        return "Familles : " + ", ".join(labels)
    return ""


def _form_content(
    request: Request,
    *,
    values: Mapping[str, Any],
    errors: list[str],
    action: str,
    title: str,
    submit_label: str,
    kind_locked: bool,
) -> str:
    parts = [f"<h1>{escape(title)}</h1>"]
    parts.append(
        "<p>Le type détermine les paramètres utilisés. Les autres champs sont "
        "ignorés. La référence figée n'est jamais modifiée par cette page.</p>"
    )
    if errors:
        items = "".join(f"<li>{escape(error)}</li>" for error in errors)
        parts.append(
            '<div role="alert"><p>La règle n\'a pas été enregistrée :</p>'
            f"<ul>{items}</ul></div>"
        )

    parts.append(f'<form method="post" action="{escape(action)}">')
    parts.append(_csrf_input(request.csrf_token))

    parts.append(
        '<p><label for="rule-name">Nom de la règle</label><br>'
        f'<input id="rule-name" name="name" required maxlength="'
        f'{MAX_RULE_NAME_LENGTH}" value="{escape(values.get("name", ""))}"></p>'
    )

    current_kind = str(values.get("kind", ""))
    if kind_locked:
        label = RULE_KIND_LABELS.get(current_kind, current_kind)
        parts.append(
            "<p>Type : "
            f"<strong>{escape(label)}</strong> "
            f"(<code>{escape(current_kind)}</code>)</p>"
        )
        parts.append(
            f'<input type="hidden" name="kind" value="{escape(current_kind)}">'
        )
    else:
        parts.append(
            '<p><label for="rule-kind">Type de règle</label><br>'
            '<select id="rule-kind" name="kind">'
        )
        for kind in RULE_KINDS:
            selected = " selected" if kind == current_kind else ""
            parts.append(
                f'<option value="{escape(kind)}"{selected}>'
                f"{escape(RULE_KIND_LABELS.get(kind, kind))}</option>"
            )
        parts.append("</select></p>")

    checked = " checked" if values.get("enabled") else ""
    parts.append(
        '<p><label><input type="checkbox" name="enabled" value="1"'
        f"{checked}> Règle active</label></p>"
    )

    parts.append("<fieldset><legend>Bloc d'annonce de fermeture</legend>")
    parts.append(
        '<p><label for="rule-anchor">Ancre du bloc</label><br>'
        '<input id="rule-anchor" name="anchor" maxlength="160" '
        f'value="{escape(values.get("anchor", ""))}"></p>'
        "<p>Texte spécifique qui identifie l'annonce de fermeture, par exemple "
        "« Complet 2026 ». Les mots génériques comme « rendez-vous » ou "
        "« calendrier » sont refusés.</p>"
    )
    parts.append("</fieldset>")

    parts.append("<fieldset><legend>Horodatage de mise à jour</legend>")
    parts.append(
        '<p><label for="rule-label">Libellé de l\'horodatage</label><br>'
        '<input id="rule-label" name="label" maxlength="160" '
        f'value="{escape(values.get("label", ""))}"></p>'
        '<p><label for="rule-window">Fenêtre de recherche (caractères)</label>'
        "<br>"
        '<input id="rule-window" name="window" type="number" min="1" '
        f'max="{MAX_TIMESTAMP_WINDOW}" '
        f'value="{escape(values.get("window", ""))}"></p>'
    )
    parts.append("</fieldset>")

    parts.append("<fieldset><legend>Nouvel élément absent de la référence</legend>")
    selected_sources = set(values.get("sources") or [])
    for source in ELEMENT_SOURCES:
        checked = " checked" if source in selected_sources else ""
        parts.append(
            f'<label><input type="checkbox" name="sources" '
            f'value="{escape(source)}"{checked}> '
            f"{escape(SOURCE_LABELS.get(source, source))}</label><br>"
        )
    parts.append("</fieldset>")

    parts.append(
        f'<p><button type="submit">{escape(submit_label)}</button> '
        '<a href="/rules">Annuler</a></p>'
    )
    parts.append("</form>")
    return "\n".join(parts)


def _csrf_input(token: str | None) -> str:
    return (
        '<input type="hidden" name="csrf_token" '
        f'value="{escape(token or "")}">'
    )


def _invalid_form(
    request: Request,
    values: Mapping[str, Any],
    errors: list[str],
    *,
    action: str,
    kind_locked: bool = False,
) -> Response:
    title = "Modifier la règle" if kind_locked else "Ajouter une règle"
    content = _form_content(
        request,
        values=values,
        errors=errors,
        action=action,
        title=title,
        submit_label="Enregistrer les modifications" if kind_locked else "Ajouter la règle",
        kind_locked=kind_locked,
    )
    return html_response(400, render_page(title, content))


def _redirect(location: str) -> Response:
    return Response(
        status=303,
        body=b"",
        headers=[
            ("Location", location),
            ("Content-Type", "text/html; charset=utf-8"),
        ],
    )


def _not_found() -> Response:
    return html_response(
        404,
        render_page(
            "Règle introuvable",
            "<h1>Règle introuvable</h1>"
            "<p>Cette règle n'existe pas ou a été supprimée.</p>"
            '<p><a href="/rules">Retour aux règles</a></p>',
        ),
    )


def _log(
    logger: StructuredLogger | None,
    event: str,
    **fields: object,
) -> None:
    if logger is not None:
        logger.log(event, **fields)


__all__ = ["register_rule_routes"]
