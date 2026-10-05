"""Modals reached from the checklist: view details, change owners, close.

The channel post stays short — name, stakeholders, delivery date — and the full
submission lives behind a button. A wall of thirty fields in the channel is
noise for everyone who is not working that project.
"""

from __future__ import annotations

from typing import Any, Dict, List

from gates import CLOSE_REASONS, SIGNOFF_LABELS
from form_builder import is_input, load_schema, visible_fields

DETAIL_CALLBACK = "intake_detail"
EDIT_CALLBACK = "intake_edit"
OWNERS_CALLBACK = "intake_owners"
CLOSE_CALLBACK = "intake_close"

SCHEMA = load_schema()

# Shown in the channel summary; everything else waits behind the button.
HEADLINE_KEYS = ["account", "workstream", "target_date", "urgency"]


# Slack rejects the whole view if any section text exceeds this, and the failure
# lands after ack() — so the button would simply stop working, permanently and
# silently, for whichever project happened to contain a long answer.
SECTION_LIMIT = 3000


def _text(s: str, limit: int = 150) -> Dict[str, str]:
    return {"type": "plain_text", "text": str(s)[:limit], "emoji": True}


def clamp(text: str, limit: int = SECTION_LIMIT) -> str:
    """Trim to Slack's limit, leaving a visible mark that something was cut."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def mrkdwn_section(text: str) -> Dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": clamp(text)}}


def _pretty(field: Dict[str, Any], value: Any) -> str:
    options = {o["value"]: o["label"] for o in field.get("options", [])}
    if value in (None, "", []):
        return "—"
    if field["type"] == "user":
        return f"<@{value}>"
    if field["type"] == "multi_user":
        return ", ".join(f"<@{v}>" for v in value)
    if isinstance(value, list):
        return ", ".join(options.get(v, v) for v in value)
    return options.get(value, str(value))


# --------------------------------------------------------------------------
# the short channel post
# --------------------------------------------------------------------------

def summary_blocks(run_id: str, project_name: str, type_label: str, submitter: str,
                   state: Dict[str, Any], owner_keys: List[str],
                   sheet_url: str | None,
                   doc_url: str | None = None) -> List[Dict[str, Any]]:
    """Name, stakeholders, delivery date. Everything else is one click away."""
    fields_by_key = {
        f["key"]: f
        for f in SCHEMA["common_fields"]
        + SCHEMA["project_types"][_type_key(type_label)]["fields"]
    }

    owners = " · ".join(
        f"{SIGNOFF_LABELS[k]} <@{state[k]}>" for k in owner_keys if state.get(k)
    )

    headline = [
        f"*{fields_by_key[k]['label']}*  {_pretty(fields_by_key[k], state.get(k))}"
        for k in HEADLINE_KEYS
        if k in fields_by_key and state.get(k)
    ]

    blocks: List[Dict[str, Any]] = [
        mrkdwn_section(f"*{project_name}*  ·  {type_label}"),
        {"type": "context", "elements": [{"type": "mrkdwn", "text": owners or "_No owners named_"}]},
    ]
    if headline:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "   ".join(headline)}]}
        )

    buttons = [
        {
            "type": "button",
            "text": _text("View full intake"),
            "action_id": f"intake_detail:{run_id}",
            "value": "detail",
        },
        {
            "type": "button",
            "text": _text("Edit"),
            "action_id": f"intake_edit:{run_id}",
            "value": "edit",
        },
    ]
    if doc_url:
        buttons.append(
            {
                "type": "button",
                "text": _text("Project doc"),
                "url": doc_url,
                "action_id": f"intake_doc:{run_id}",
            }
        )
    if sheet_url:
        buttons.append(
            {
                "type": "button",
                "text": _text("Open sheet"),
                "url": sheet_url,
                "action_id": f"intake_sheet:{run_id}",
            }
        )
    blocks.append({"type": "actions", "elements": buttons})

    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Submitted by <@{submitter}>"}],
        }
    )
    return blocks


def _type_key(type_label: str) -> str:
    for key, defn in SCHEMA["project_types"].items():
        if defn["label"] == type_label:
            return key
    return next(iter(SCHEMA["project_types"]))


# --------------------------------------------------------------------------
# full detail, read-only
# --------------------------------------------------------------------------

def detail_view(run: Dict[str, Any]) -> Dict[str, Any]:
    record = run.get("record") or {}
    type_key = run["project_type"]
    fields = [
        f
        for f in visible_fields(SCHEMA, type_key, record)
        if is_input(f) or f["type"] == "context"
    ]

    blocks: List[Dict[str, Any]] = [
        mrkdwn_section(f"*{run['project_name']}*"),
        {"type": "divider"},
    ]

    pending: List[Dict[str, str]] = []

    def flush():
        # Ten fields per section is Slack's limit; two columns read best.
        while pending:
            blocks.append({"type": "section", "fields": pending[:10]})
            del pending[:10]

    for field in fields:
        if field["type"] == "context":
            flush()
            blocks.append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"*{field['label']}*"}],
                }
            )
            continue
        value = _pretty(field, record.get(field["key"]))
        # Long prose gets its own full-width block; short values pair up.
        if len(value) > 80:
            flush()
            blocks.append(mrkdwn_section(f"*{field['label']}*\n{value}"))
        else:
            # Fields inside a section are capped at 2000 each, not 3000.
            pending.append(
                {"type": "mrkdwn", "text": clamp(f"*{field['label']}*\n{value}", 2000)}
            )
    flush()

    return {
        "type": "modal",
        "callback_id": DETAIL_CALLBACK,
        "title": _text("Full intake", 24),
        "close": _text("Done", 24),
        "blocks": blocks[:100],
    }


# --------------------------------------------------------------------------
# change owners
# --------------------------------------------------------------------------

def owners_view(run: Dict[str, Any]) -> Dict[str, Any]:
    blocks: List[Dict[str, Any]] = [
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"*{run['project_name']}* — sign-offs already given stay recorded. "
                    "Outstanding ones move to whoever holds the role from here on.",
                }
            ],
        }
    ]
    for key, label in SIGNOFF_LABELS.items():
        element: Dict[str, Any] = {
            "type": "users_select",
            "action_id": key,
            "placeholder": _text(f"Select the {label.lower()} owner"),
        }
        if run["owners"].get(key):
            element["initial_user"] = run["owners"][key]
        blocks.append(
            {
                "type": "input",
                "block_id": key,
                "label": _text(f"{label} owner"),
                "element": element,
                "optional": True,
            }
        )

    return {
        "type": "modal",
        "callback_id": OWNERS_CALLBACK,
        "private_metadata": run["id"],
        "title": _text("Change owners", 24),
        "submit": _text("Save", 24),
        "close": _text("Cancel", 24),
        "blocks": blocks,
    }


# --------------------------------------------------------------------------
# close
# --------------------------------------------------------------------------

def close_view(run: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": CLOSE_CALLBACK,
        "private_metadata": run["id"],
        "title": _text("Close project", 24),
        "submit": _text("Close", 24),
        "close": _text("Cancel", 24),
        "blocks": [
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"*{run['project_name']}* — closing stops the checklist. "
                        "Nothing is deleted, and it can be reopened with every tick intact.",
                    }
                ],
            },
            {
                "type": "input",
                "block_id": "reason",
                "label": _text("Why is it closing?"),
                "element": {
                    "type": "radio_buttons",
                    "action_id": "reason",
                    "options": [
                        {"text": _text(label, 75), "value": key} for key, label in CLOSE_REASONS
                    ],
                },
            },
            {
                "type": "input",
                "block_id": "note",
                "optional": True,
                "label": _text("Anything worth recording?"),
                "element": {
                    "type": "plain_text_input",
                    "action_id": "note",
                    "multiline": True,
                    "placeholder": _text("Optional — what the team should know later"),
                },
            },
        ],
    }
