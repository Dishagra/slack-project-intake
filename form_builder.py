"""Schema -> Block Kit modal, and modal state -> plain dict.

The whole form lives in form_schema.json. This module is the only place that
knows how to turn a field spec into a Block Kit element, so adding a new field
type means adding one branch to _element() and one branch to _read_value().
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from typing import Any, Dict, List, Optional

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "form_schema.json")

CALLBACK_ID = "project_intake"
TYPE_KEY = "project_type"

# Field types that render as labels rather than inputs: they carry no value,
# are never required, and never reach the sheet.
DISPLAY_TYPES = {"context", "divider"}


def is_input(field: Dict[str, Any]) -> bool:
    return field["type"] not in DISPLAY_TYPES

# Slack caps a modal at 100 blocks; we leave headroom for the type picker,
# divider and context blocks we add ourselves.
MAX_FIELD_BLOCKS = 90


def load_schema(path: str = SCHEMA_PATH) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------
# field graph
# --------------------------------------------------------------------------

def all_fields(schema: Dict[str, Any], project_type: Optional[str]) -> List[Dict[str, Any]]:
    """Common fields followed by the chosen type's fields. Empty if no type yet."""
    if not project_type:
        return []
    type_def = schema["project_types"].get(project_type)
    if type_def is None:
        return []
    return list(schema.get("common_fields", [])) + list(type_def.get("fields", []))


def trigger_keys(schema: Dict[str, Any]) -> set:
    """Keys that some other field's show_if depends on.

    Covers both show_if and required_if. These get dispatch_action=True so
    changing them re-renders the modal.
    Derived, not hand-maintained, so schema edits can't forget to set a flag.
    """
    keys = set()
    for type_name in schema["project_types"]:
        for field in all_fields(schema, type_name):
            # required_if counts too: turning a field from optional to mandatory
            # changes the block Slack renders, so the form has to be redrawn.
            for key in ("show_if", "required_if"):
                cond = field.get(key)
                if cond:
                    keys.add(cond["field"])
    return keys


def _matches(cond: Optional[Dict[str, Any]], state: Dict[str, Any]) -> bool:
    """Whether a {field, in} condition holds against the answers so far."""
    if not cond:
        return True
    current = state.get(cond["field"])
    if current is None:
        return False
    if isinstance(current, list):
        return any(v in cond["in"] for v in current)
    return current in cond["in"]


def _is_visible(field: Dict[str, Any], state: Dict[str, Any]) -> bool:
    return _matches(field.get("show_if"), state)


def is_required(field: Dict[str, Any], state: Dict[str, Any]) -> bool:
    """Whether this field must be answered, given everything answered so far.

    Most of the form describes work that may not have started, so it is optional
    by default. `required_if` covers the case where an earlier answer changes
    that: saying research is underway is a claim that the research answers exist.
    """
    if not field.get("optional"):
        return True
    return bool(field.get("required_if")) and _matches(field["required_if"], state)


def visible_fields(
    schema: Dict[str, Any], project_type: Optional[str], state: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Fields to render, honouring show_if chains.

    Evaluated in order so a field can depend on one above it; a field whose
    controller is itself hidden stays hidden (its controller's value is dropped).
    """
    live: Dict[str, Any] = {}
    out: List[Dict[str, Any]] = []
    for field in all_fields(schema, project_type):
        if _is_visible(field, live):
            out.append(field)
            live[field["key"]] = state.get(field["key"])
    return out


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def valid_initial(field_type: str, value: Any) -> bool:
    """Whether Slack will accept this value as an initial_value.

    url_text_input and email_text_input validate their initial_value and reject
    the whole views.update if it does not parse. A half-typed link is completely
    normal mid-form — the user types "docs.google.com/..." and then changes a
    field that triggers a re-render — so this has to be checked before sending,
    not discovered as an API error afterwards.
    """
    if not value:
        return False
    if field_type == "url":
        try:
            parsed = urllib.parse.urlparse(str(value))
        except ValueError:
            return False
        return bool(parsed.scheme and parsed.netloc)
    if field_type == "email":
        return bool(_EMAIL_RE.match(str(value)))
    return True


def _text(s: str, max_len: int = 150) -> Dict[str, str]:
    return {"type": "plain_text", "text": s[:max_len], "emoji": True}


def _options(field: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {"text": _text(o["label"], 75), "value": o["value"]}
        for o in field.get("options", [])
    ]


def _option_by_value(field: Dict[str, Any], value: str) -> Optional[Dict[str, Any]]:
    for opt in _options(field):
        if opt["value"] == value:
            return opt
    return None


def _element(field: Dict[str, Any], value: Any) -> Dict[str, Any]:
    ftype = field["type"]
    el: Dict[str, Any] = {"action_id": field["key"]}

    if ftype in ("text", "textarea", "url", "email"):
        el["type"] = {
            "text": "plain_text_input",
            "textarea": "plain_text_input",
            "url": "url_text_input",
            "email": "email_text_input",
        }[ftype]
        if ftype == "textarea":
            el["multiline"] = True
        if field.get("placeholder"):
            el["placeholder"] = _text(field["placeholder"])
        if value and valid_initial(ftype, value):
            el["initial_value"] = str(value)

    elif ftype == "number":
        el["type"] = "number_input"
        el["is_decimal_allowed"] = bool(field.get("decimal", False))
        for bound in ("min_value", "max_value"):
            if field.get(bound) is not None:
                el[bound] = str(field[bound])
        if value not in (None, ""):
            el["initial_value"] = str(value)

    elif ftype in ("select", "radio"):
        el["type"] = "static_select" if ftype == "select" else "radio_buttons"
        el["options"] = _options(field)
        if ftype == "select":
            el["placeholder"] = _text(field.get("placeholder", "Select one"))
        if value:
            opt = _option_by_value(field, value)
            if opt:
                el["initial_option"] = opt

    elif ftype in ("multi_select", "checkboxes"):
        el["type"] = "multi_static_select" if ftype == "multi_select" else "checkboxes"
        el["options"] = _options(field)
        if ftype == "multi_select":
            el["placeholder"] = _text(field.get("placeholder", "Select all that apply"))
        chosen = [o for v in (value or []) if (o := _option_by_value(field, v))]
        if chosen:
            el["initial_options"] = chosen

    elif ftype == "date":
        el["type"] = "datepicker"
        el["placeholder"] = _text(field.get("placeholder", "Pick a date"))
        if value:
            el["initial_date"] = value

    elif ftype == "user":
        el["type"] = "users_select"
        el["placeholder"] = _text(field.get("placeholder", "Select a person"))
        if value:
            el["initial_user"] = value

    elif ftype == "multi_user":
        el["type"] = "multi_users_select"
        el["placeholder"] = _text(field.get("placeholder", "Select people"))
        if value:
            el["initial_users"] = value

    else:
        raise ValueError(f"unknown field type: {ftype!r} (field {field['key']!r})")

    return el


def _block(field: Dict[str, Any], value: Any, dispatch: bool,
           state: Dict[str, Any]) -> Dict[str, Any]:
    if field["type"] == "divider":
        return {"type": "divider"}
    if field["type"] == "context":
        return {
            "type": "context",
            "block_id": field["key"],
            "elements": [{"type": "mrkdwn", "text": f"*{field['label']}*"}],
        }

    block: Dict[str, Any] = {
        "type": "input",
        "block_id": field["key"],
        "label": _text(field["label"], 2000),
        "element": _element(field, value),
        "optional": not is_required(field, state),
    }

    hint = field.get("hint")
    if value and not valid_initial(field["type"], value):
        # The value could not be carried through the re-render. Say so, rather
        # than letting the field look mysteriously empty.
        cleared = (
            f'Cleared "{value}" — needs a full address like https://example.com'
            if field["type"] == "url"
            else f'Cleared "{value}" — needs a full email address'
        )
        hint = f"{hint} {cleared}" if hint else cleared
    if hint:
        block["hint"] = _text(hint, 2000)
    if dispatch:
        block["dispatch_action"] = True
    return block


def _type_picker(schema: Dict[str, Any], project_type: Optional[str]) -> Dict[str, Any]:
    options = [
        {"text": _text(defn["label"], 75), "value": name}
        for name, defn in schema["project_types"].items()
    ]
    element: Dict[str, Any] = {
        "type": "static_select",
        "action_id": TYPE_KEY,
        "placeholder": _text("Select a project type"),
        "options": options,
    }
    if project_type:
        match = next((o for o in options if o["value"] == project_type), None)
        if match:
            element["initial_option"] = match
    return {
        "type": "input",
        "block_id": TYPE_KEY,
        "label": _text("Project type"),
        "element": element,
        "dispatch_action": True,
    }


def build_view(
    schema: Dict[str, Any],
    project_type: Optional[str],
    state: Dict[str, Any],
    private_metadata: str = "",
    callback_id: str = CALLBACK_ID,
    submit_label: Optional[str] = None,
) -> Dict[str, Any]:
    """Full modal view for the current type + answers so far.

    callback_id decides which handler receives the submission, so the same form
    serves both a new intake and an edit of an existing one.
    """
    triggers = trigger_keys(schema)
    blocks: List[Dict[str, Any]] = [_type_picker(schema, project_type)]

    if project_type:
        blocks.append({"type": "divider"})
        fields = visible_fields(schema, project_type, state)
        if len(fields) > MAX_FIELD_BLOCKS:
            raise ValueError(
                f"{len(fields)} visible fields exceeds the {MAX_FIELD_BLOCKS}-block "
                "modal budget; split this project type into two forms"
            )
        for field in fields:
            blocks.append(
                _block(field, state.get(field["key"]), field["key"] in triggers, state)
            )
    else:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "_Pick a project type — the rest of the form loads from it._",
                    }
                ],
            }
        )

    view: Dict[str, Any] = {
        "type": "modal",
        "callback_id": callback_id,
        "title": _text(
            schema.get("title", "New Project") if callback_id == CALLBACK_ID else "Edit intake",
            24,
        ),
        "close": _text("Cancel", 24),
        "blocks": blocks,
        "private_metadata": private_metadata,
    }
    if project_type:
        view["submit"] = _text(submit_label or schema.get("submit_label", "Submit"), 24)
    return view


# --------------------------------------------------------------------------
# reading state back
# --------------------------------------------------------------------------

def _read_value(payload: Dict[str, Any]) -> Any:
    """One element's state payload -> str | list[str] | None."""
    etype = payload.get("type")
    if etype in ("plain_text_input", "url_text_input", "email_text_input", "number_input"):
        return payload.get("value") or None
    if etype in ("static_select", "radio_buttons", "external_select"):
        opt = payload.get("selected_option")
        return opt["value"] if opt else None
    if etype in ("multi_static_select", "checkboxes"):
        return [o["value"] for o in payload.get("selected_options") or []] or None
    if etype == "datepicker":
        return payload.get("selected_date")
    if etype == "users_select":
        return payload.get("selected_user")
    if etype == "multi_users_select":
        return payload.get("selected_users") or None
    return None


def extract_state(view: Dict[str, Any]) -> Dict[str, Any]:
    """view.state.values -> {field_key: value}.

    block_id and action_id are both the field key, so the nesting collapses.
    Values for fields that are currently hidden simply aren't present — Slack
    only reports rendered blocks — which is what makes re-render safe.
    """
    out: Dict[str, Any] = {}
    for block_id, actions in (view.get("state", {}).get("values") or {}).items():
        for action_id, payload in actions.items():
            value = _read_value(payload)
            if value is not None:
                out[action_id or block_id] = value
    return out
