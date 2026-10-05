"""Opportunity Ingestion: Delivery's intake check, before any work starts.

From the Opportunity Ingestion Checklist. Every new opportunity — internal or
customer-driven — has to give Delivery six things before work starts, and Delivery
owes a go/no-go within 36 hours of the request. The rules, as the doc states them:

  - Each element is signed off individually, with a date.
  - Delivery comes back with clarifications and a go/no-go within 36 hours.
  - Final sign-off is between the requestor and the Delivery owner.
  - Recurring accounts are ingested once, not on every scope change.

Who does what, enforced rather than trusted: only the Delivery owner signs off
elements and makes the call; each final sign-off is accepted only from the person
named in that role. Changing an element after it was signed off clears that
sign-off, because Delivery signed off on what they read.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

import gates

SLA_HOURS = 36
WARN_HOURS = 30  # nudge before the deadline, not only after missing it

ELEMENTS: List[Dict[str, Any]] = [
    {"key": "spec_doc", "label": "Specifications document"},
    {"key": "task_volume", "label": "Volume (number of tasks)"},
    {"key": "example_task", "label": "Example task, or customer spec doc", "optional": True},
    {"key": "crucial_details", "label": "Other crucial details", "optional": True},
    {"key": "tat_agreed", "label": "TAT agreed upon"},
    {"key": "milestones", "label": "Progress milestones aligned"},
]
ELEMENT_KEYS = [e["key"] for e in ELEMENTS]

# The final sign-off pair, per the doc: requestor and Delivery owner.
SIGNERS = {"requestor": "Requestor", "delivery_owner": "Delivery owner"}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(moment: dt.datetime) -> str:
    return moment.isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# reading state
# --------------------------------------------------------------------------

def needed(record: Dict[str, Any]) -> bool:
    """Recurring accounts were ingested once already; everything else is new."""
    return record.get("ingestion_kind") != "recurring"


def fresh() -> Dict[str, Any]:
    return {
        "requested_at": _iso(_now()),
        "signed": {},
        "signoffs": {},
        "decision": None,
        "questions": [],
        "nudges": [],
        "message_ts": None,
    }


def of(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The ingestion block, or None for a run that never needed one."""
    return state.get("ingestion")


def complete(state: Dict[str, Any]) -> bool:
    """Go decided and both people signed. A run without ingestion counts as done."""
    ing = of(state)
    if ing is None:
        return True
    decision = ing.get("decision") or {}
    return decision.get("value") == "go" and all(k in ing["signoffs"] for k in SIGNERS)


def hours_elapsed(state: Dict[str, Any], now: Optional[dt.datetime] = None) -> Optional[float]:
    ing = of(state)
    if not ing:
        return None
    started = dt.datetime.fromisoformat(ing["requested_at"])
    return ((now or _now()) - started).total_seconds() / 3600


def deadline(state: Dict[str, Any]) -> Optional[dt.datetime]:
    ing = of(state)
    if not ing:
        return None
    return dt.datetime.fromisoformat(ing["requested_at"]) + dt.timedelta(hours=SLA_HOURS)


def awaiting_decision(state: Dict[str, Any]) -> bool:
    ing = of(state)
    return bool(ing) and not ing.get("decision") and not state.get("closed")


def status(state: Dict[str, Any], now: Optional[dt.datetime] = None) -> Optional[str]:
    """One line for the sheet while ingestion is still open, else None."""
    ing = of(state)
    if not ing or complete(state):
        return None
    signed = sum(1 for k in ELEMENT_KEYS if k in ing["signed"])
    line = f"Ingestion · {signed}/{len(ELEMENT_KEYS)} signed off"
    decision = (ing.get("decision") or {}).get("value")
    if decision == "go":
        missing = [label for key, label in SIGNERS.items() if key not in ing["signoffs"]]
        return f"{line} · go — awaiting {' and '.join(missing)} sign-off"
    elapsed = hours_elapsed(state, now) or 0
    if elapsed >= SLA_HOURS:
        return f"{line} · go/no-go overdue by {elapsed - SLA_HOURS:.0f}h"
    return f"{line} · go/no-go due in {SLA_HOURS - elapsed:.0f}h"


# --------------------------------------------------------------------------
# changing state — every mutation goes through gates._mutate, so it is atomic
# --------------------------------------------------------------------------

def start(run_id: str) -> Dict[str, Any]:
    def change(state, owners):
        state.setdefault("ingestion", fresh())
        return state

    return gates._mutate(run_id, change)


def set_message_ts(run_id: str, ts: str) -> None:
    def change(state, owners):
        state["ingestion"]["message_ts"] = ts
        return state

    gates._mutate(run_id, change)


def _refuse_unless_delivery(state, owners, user_id) -> Optional[str]:
    if state.get("closed"):
        return "This project is closed. Reopen it before changing the ingestion."
    expected = owners.get("delivery_owner")
    if expected and user_id != expected:
        return (f"Only the Delivery owner, <@{expected}>, signs off ingestion elements "
                f"and makes the go/no-go call.")
    return None


def set_signed(run_id: str, keys: List[str], user_id: str) -> Tuple[bool, str]:
    """Replace the element sign-offs with what the checkbox group now reports."""
    outcome: Dict[str, Any] = {"ok": False, "message": ""}

    def change(state, owners):
        refusal = _refuse_unless_delivery(state, owners, user_id)
        if refusal:
            outcome["message"] = refusal
            return state
        ing = state["ingestion"]
        if (ing.get("decision") or {}).get("value") == "go":
            outcome["message"] = "The go decision is already made. Elements are locked."
            return state
        when = _iso(_now())
        ing["signed"] = {
            k: ing["signed"].get(k) or {"by": user_id, "at": when}
            for k in ELEMENT_KEYS if k in keys
        }
        outcome["ok"] = True
        return state

    gates._mutate(run_id, change)
    return outcome["ok"], outcome["message"]


def decide(run_id: str, value: str, user_id: str, note: str = "") -> Tuple[bool, str]:
    """Go or no-go. Go needs every element signed off; no-go needs a reason."""
    outcome: Dict[str, Any] = {"ok": False, "message": ""}

    def change(state, owners):
        refusal = _refuse_unless_delivery(state, owners, user_id)
        if refusal:
            outcome["message"] = refusal
            return state
        ing = state["ingestion"]
        if ing.get("decision"):
            outcome["message"] = "The go/no-go call has already been made."
            return state
        if value == "go":
            missing = [e["label"] for e in ELEMENTS if e["key"] not in ing["signed"]]
            if missing:
                outcome["message"] = "Sign off every element before calling go: " + ", ".join(missing)
                return state
        elif value == "no_go" and not note.strip():
            outcome["message"] = "A no-go needs a reason the requestor can act on."
            return state
        ing["decision"] = {"value": value, "by": user_id, "at": _iso(_now()), "note": note}
        outcome["ok"] = True
        return state

    gates._mutate(run_id, change)
    return outcome["ok"], outcome["message"]


def sign_off(run_id: str, role: str, user_id: str) -> Tuple[bool, str, bool]:
    """Final sign-off. Returns ok, message, and whether ingestion just completed."""
    outcome: Dict[str, Any] = {"ok": False, "message": "", "completed": False}

    def change(state, owners):
        if state.get("closed"):
            outcome["message"] = "This project is closed."
            return state
        ing = state["ingestion"]
        if (ing.get("decision") or {}).get("value") != "go":
            outcome["message"] = "Final sign-off comes after Delivery calls go."
            return state
        expected = owners.get(role)
        if expected and user_id != expected:
            outcome["message"] = (f"Only <@{expected}> can give the {SIGNERS[role]} sign-off. "
                                  f"Use *Change owners* if the role has moved.")
            return state
        ing["signoffs"][role] = {"by": user_id, "at": _iso(_now())}
        outcome["ok"] = True
        outcome["message"] = f"{SIGNERS[role]} sign-off recorded."
        outcome["completed"] = complete(state)
        return state

    gates._mutate(run_id, change)
    return outcome["ok"], outcome["message"], outcome["completed"]


def ask(run_id: str, user_id: str, question: str) -> None:
    def change(state, owners):
        state["ingestion"]["questions"].append(
            {"by": user_id, "at": _iso(_now()), "text": question}
        )
        return state

    gates._mutate(run_id, change)


def invalidate(run_id: str, changed: List[str]) -> List[str]:
    """Clear sign-offs on elements an edit changed. Returns which were cleared.

    Delivery signed off on what they read. If the spec doc link or the volume
    changes afterwards, that sign-off no longer says anything about the new value.
    Once go is called the ingestion is settled and edits leave it alone.
    """
    cleared: List[str] = []

    def change(state, owners):
        ing = state.get("ingestion")
        if not ing or (ing.get("decision") or {}).get("value") == "go":
            return state
        for key in changed:
            if key in ing["signed"]:
                ing["signed"].pop(key)
                cleared.append(key)
        return state

    gates._mutate(run_id, change)
    return cleared


def due_nudges(state: Dict[str, Any], now: Optional[dt.datetime] = None) -> List[str]:
    """Which reminders are owed and not yet sent: 'warn' at 30h, 'breach' at 36h."""
    if not awaiting_decision(state):
        return []
    elapsed = hours_elapsed(state, now) or 0
    sent = set(state["ingestion"]["nudges"])
    owed = []
    if elapsed >= WARN_HOURS and "warn" not in sent and elapsed < SLA_HOURS:
        owed.append("warn")
    if elapsed >= SLA_HOURS and "breach" not in sent:
        owed.append("breach")
    return owed


def record_nudge(run_id: str, kind: str) -> None:
    def change(state, owners):
        state["ingestion"]["nudges"].append(kind)
        return state

    gates._mutate(run_id, change)
