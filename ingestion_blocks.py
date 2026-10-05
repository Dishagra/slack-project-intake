"""The ingestion message: what Delivery checks before work starts.

Posted in the thread the moment a new opportunity is submitted, and rewritten in
place as Delivery signs off elements, asks questions and makes the call. The
Steps 2-6 checklist only appears after this one is fully signed off, because none
of it means anything until Delivery has accepted the work.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List

import ingestion
from admin_views import clamp

CHECK = "ingest_check"
GO = "ingest_go"
NO_GO = "ingest_nogo"
ASK = "ingest_ask"
SIGN = "ingest_sign"

NO_GO_CALLBACK = "ingest_nogo_modal"
ASK_CALLBACK = "ingest_ask_modal"


def _when(iso: str) -> str:
    moment = dt.datetime.fromisoformat(iso).astimezone()
    return moment.strftime("%a %d %b, %H:%M")


def _clock(state: Dict[str, Any]) -> str:
    ing = ingestion.of(state)
    decision = ing.get("decision") or {}
    if decision.get("value") == "go":
        return f"✅ *Go* — {_when(decision['at'])} by <@{decision['by']}>"
    if decision.get("value") == "no_go":
        return f"⛔ *No-go* — {_when(decision['at'])} by <@{decision['by']}>"
    elapsed = ingestion.hours_elapsed(state) or 0
    due = _when(ingestion.deadline(state).isoformat())
    if elapsed >= ingestion.SLA_HOURS:
        return f"🔴 *Go/no-go overdue* by {elapsed - ingestion.SLA_HOURS:.0f}h — was due {due}"
    left = ingestion.SLA_HOURS - elapsed
    icon = "🟠" if elapsed >= ingestion.WARN_HOURS else "⏳"
    return f"{icon} Go/no-go due *{due}* — {left:.0f}h left of {ingestion.SLA_HOURS}h"


def blocks(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    state, owners, record = run["state"], run["owners"], run.get("record") or {}
    ing = ingestion.of(state)
    run_id = run["id"]
    decided = bool(ing.get("decision"))

    people = " · ".join(
        f"{label} <@{owners[key]}>" for key, label in ingestion.SIGNERS.items() if owners.get(key)
    )
    out: List[Dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn",
                                     "text": clamp(f"*Opportunity ingestion* — {run['project_name']}")}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": people or "_No owners named_"}]},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": _clock(state)}]},
        {"type": "divider"},
    ]

    # The six elements, each with its value and whether Delivery has signed it.
    for element in ingestion.ELEMENTS:
        value = record.get(element["key"])
        shown = str(value).strip() if value not in (None, "", []) else "_not provided_"
        signed = ing["signed"].get(element["key"])
        mark = f"  ✓ _signed {_when(signed['at'])}_" if signed else ""
        out.append({"type": "section", "text": {"type": "mrkdwn",
                                                "text": clamp(f"*{element['label']}*{mark}\n{shown}")}})

    if state.get("closed"):
        out.append({"type": "context", "elements": [{"type": "mrkdwn",
                    "text": f"🔒 Closed — {state['closed']['reason']}"}]})
        return out

    if not decided:
        options = [{"text": {"type": "plain_text", "text": e["label"][:75]}, "value": e["key"]}
                   for e in ingestion.ELEMENTS]
        element = {"type": "checkboxes", "action_id": f"{CHECK}:{run_id}", "options": options}
        initial = [o for o in options if o["value"] in ing["signed"]]
        if initial:
            element["initial_options"] = initial
        out.append({"type": "divider"})
        out.append({"type": "context", "elements": [{"type": "mrkdwn",
                    "text": "*Delivery owner:* sign off each element you've checked."}]})
        out.append({"type": "actions", "elements": [element]})

    questions = ing.get("questions") or []
    if questions:
        last = questions[-1]
        out.append({"type": "context", "elements": [{"type": "mrkdwn", "text": clamp(
            f"💬 {len(questions)} question(s) to the requestor — latest from <@{last['by']}>: "
            f"_{last['text']}_", 3000)}]})

    decision = ing.get("decision") or {}
    if decision.get("value") == "no_go" and decision.get("note"):
        out.append({"type": "section", "text": {"type": "mrkdwn",
                    "text": clamp(f"*Why no-go*\n{decision['note']}")}})

    buttons: List[Dict[str, Any]] = []
    if not decided:
        buttons += [
            {"type": "button", "style": "primary", "text": {"type": "plain_text", "text": "Go"},
             "action_id": f"{GO}:{run_id}", "value": "go"},
            {"type": "button", "style": "danger", "text": {"type": "plain_text", "text": "No-go"},
             "action_id": f"{NO_GO}:{run_id}", "value": "no_go"},
            {"type": "button", "text": {"type": "plain_text", "text": "Ask the requestor"},
             "action_id": f"{ASK}:{run_id}", "value": "ask"},
        ]
    elif decision.get("value") == "go":
        for role, label in ingestion.SIGNERS.items():
            done = ing["signoffs"].get(role)
            if done:
                out.append({"type": "context", "elements": [{"type": "mrkdwn",
                            "text": f"✍️ {label} signed off — <@{done['by']}>, {_when(done['at'])}"}]})
            else:
                buttons.append({"type": "button",
                                "text": {"type": "plain_text", "text": f"{label} sign-off"},
                                "action_id": f"{SIGN}:{run_id}:{role}", "value": role})
    if buttons:
        out.append({"type": "actions", "elements": buttons})

    if ingestion.complete(state):
        out.append({"type": "section", "text": {"type": "mrkdwn",
                    "text": "✅ *Ingestion signed off.* Work can start."}})
    return out


def _modal(callback: str, run_id: str, title: str, label: str, submit: str,
           placeholder: str) -> Dict[str, Any]:
    return {
        "type": "modal", "callback_id": callback, "private_metadata": run_id,
        "title": {"type": "plain_text", "text": title[:24]},
        "submit": {"type": "plain_text", "text": submit[:24]},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [{
            "type": "input", "block_id": "text",
            "label": {"type": "plain_text", "text": label},
            "element": {"type": "plain_text_input", "action_id": "text", "multiline": True,
                        "placeholder": {"type": "plain_text", "text": placeholder[:150]}},
        }],
    }


def no_go_view(run_id: str) -> Dict[str, Any]:
    return _modal(NO_GO_CALLBACK, run_id, "No-go", "Why isn't Delivery taking this on?",
                  "Record no-go", "What's missing or wrong — the requestor needs to act on this")


def ask_view(run_id: str) -> Dict[str, Any]:
    return _modal(ASK_CALLBACK, run_id, "Ask the requestor", "Your question",
                  "Post question", "e.g. The spec has no acceptance criteria — what does pass look like?")
