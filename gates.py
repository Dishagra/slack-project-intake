"""Steps 2-6 of the Sample Creation Checklist, as a tracked thread.

The checklist is a sequence of gates, not a form: each step unlocks only when the
one before it is complete, and two of them need a named person to sign off. This
module owns the gate definitions and their persistence; app.py owns the Slack
plumbing.

Real work overlaps, so steps are not forced into single file. The checklist only
forbids two things outright, and those are the only two hard gates:

  - Step 4 Expand stays locked until the alignment gate passes ("never expand
    past an unvetted seed set").
  - Ship stays blocked until 24 hours after the audit request went up ("never
    the same day") and until every item is ticked ("if any answer is no, it
    does not ship").

Everything else can be worked in parallel — drafting the audit request while
expanding, writing reports while the audit runs — which is how it actually goes.

Also enforced:
  - A no-go at the alignment gate reopens Step 2 and clears its ticks, rather
    than letting the run continue ("do not patch the samples and continue").
  - Sign-off buttons only accept the person named as that owner right now.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import threading
import uuid
from typing import Any, Dict, List, Optional, Tuple

DB_PATH = os.environ.get("GATES_DB", os.path.join(os.path.dirname(__file__), "gates.db"))
AUDIT_CHANNEL = os.environ.get("AUDIT_CHANNEL", "#data-audit-champions")

# Minimum time auditors get between the request being posted and ship.
AUDIT_MIN_HOURS = 24
AUDIT_IDEAL_HOURS = 48

# Project types that run the checklist. Pilot and production have their own
# delivery process; only samples go through these gates today.
GATED_TYPES = {"sample"}


STEPS: List[Dict[str, Any]] = [
    {
        "key": "seed",
        "number": 2,
        "title": "Seed set (2–5 samples)",
        "owner": "Research and Delivery, jointly",
        "items": [
            {"key": "fidelity", "text": "Seed samples built at full shipping fidelity, not draft quality"},
            {"key": "surfaces", "text": "Each one demonstrably surfaces the Step 1 failure pattern"},
            {"key": "reference", "text": "Reference sample written, with rubric and verifiers"},
        ],
        "output": "Seed set plus reference sample, ready for the alignment gate.",
    },
    {
        "key": "alignment",
        "number": 3,
        "title": "Alignment gate",
        "owner": "Research and Delivery sign off jointly",
        "question": "Is this the right approach?",
        "items": [
            {"key": "quality_bar", "text": "Seed samples surface the pattern, at the quality bar, in a form the customer can read"},
            {"key": "second_eyes", "text": f"Second pair of eyes raised in {AUDIT_CHANNEL}"},
        ],
        "signoffs": ["research_owner", "delivery_owner"],
        "decision": True,
        "output": "Go or no go. Never expand past an unvetted seed set.",
    },
    {
        "key": "expand",
        "number": 4,
        "title": "Expand",
        "owner": "Delivery",
        # The one hard ordering rule in the checklist.
        "blocked_by": "alignment",
        "items": [
            {"key": "layered_qc", "text": "Layered QC in place before scaling volume"},
            {"key": "repeatable", "text": "Full set shows a repeatable failure pattern, not contrived diversity"},
            {"key": "synthetic", "text": "Synthetic content under 70%, human-in-the-loop role documented and justified"},
        ],
        "output": "Full sample set, ready for formal audit.",
    },
    {
        "key": "audit",
        "number": 5,
        "title": "Quality audit (formal, mandatory)",
        "owner": "PM or APM raises it; Research and Delivery both sign off",
        "items": [
            {"key": "posted", "text": f"Request posted in {AUDIT_CHANNEL} with an open-access sheet", "starts_clock": True},
            {"key": "spec", "text": "Spec doc attached — the same one used for the T&Q independent audit"},
            {"key": "reviewers", "text": "Specific skills or reviewers tagged, if the task needs them"},
            {"key": "flags", "text": "All flags addressed by the PM or APM"},
        ],
        "signoffs": ["research_owner", "delivery_owner"],
        "output": "Audited, signed-off sample set.",
    },
    {
        "key": "ship",
        "number": 6,
        "title": "Ship",
        "owner": "Research owns the insights report; Delivery owns the navigation report",
        "items": [
            {"key": "insights", "text": "Insights report complete — it argues why this data matters"},
            {"key": "navigation", "text": "Navigation report complete, self-contained, zero setup for the customer"},
            {"key": "both_ready", "text": "Both reports ready. Neither ships alone"},
        ],
        "output": "Delivered sample set, with both reports.",
    },
]

STEP_BY_KEY = {s["key"]: s for s in STEPS}
SIGNOFF_LABELS = {
    "requestor": "Requestor",
    "research_owner": "Research",
    "delivery_owner": "Delivery",
    "sales_owner": "Sales",
}


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id           TEXT PRIMARY KEY,
                channel      TEXT NOT NULL,
                thread_ts    TEXT NOT NULL,
                message_ts   TEXT,
                project_name TEXT,
                project_type TEXT,
                owners       TEXT NOT NULL,
                state        TEXT NOT NULL,
                created_at   TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS runs_thread ON runs (channel, thread_ts)")
        # The full submission, so the channel post can stay short and open the
        # detail on demand. Added after the first release; guard for old files.
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
        if "record" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN record TEXT")
        # The collaboration doc, added later still; guard for older files.
        for column in ("doc_id", "doc_url"):
            if column not in columns:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {column} TEXT")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def create_run(channel: str, thread_ts: str, project_name: str, project_type: str,
               owners: Dict[str, str], record: Optional[Dict[str, Any]] = None) -> str:
    # The prefix keeps ids non-numeric. An all-digit hex id round-trips through
    # Sheets as a number ("012345678901" reads back "12345678901") and the row
    # can never be found again — about 1 in 137 bare ids would hit that.
    run_id = "run_" + uuid.uuid4().hex[:12]
    state = {"checked": {}, "signoffs": {}, "audit_posted_at": None, "returned": []}
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO runs (id, channel, thread_ts, project_name, project_type,"
            " owners, state, created_at, record) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, channel, thread_ts, project_name, project_type,
             json.dumps(owners), json.dumps(state), _now(), json.dumps(record or {})),
        )
    return run_id


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    run = dict(row)
    run["owners"] = json.loads(run["owners"])
    run["state"] = json.loads(run["state"])
    run["record"] = json.loads(run["record"] or "{}")
    return run


def save_state(run_id: str, state: Dict[str, Any]) -> None:
    with _lock, _connect() as conn:
        conn.execute("UPDATE runs SET state = ? WHERE id = ?", (json.dumps(state), run_id))


class RunGone(Exception):
    """The run is not in the database — usually a checklist older than the file."""


def _mutate(run_id: str, change) -> Dict[str, Any]:
    """Read, change and write a run's state without letting go of the lock.

    Every mutator rewrites the whole state blob, so a read and a write that are
    separately locked can silently drop the other click's work — the losing
    update simply never happened, with nothing to notice it by. Holding the lock
    across the pair, inside one immediate transaction, makes the mutators
    serialise against each other.
    """
    with _lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state, owners FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                raise RunGone(run_id)

            state = json.loads(row["state"])
            owners = json.loads(row["owners"])
            result = change(state, owners)

            conn.execute(
                "UPDATE runs SET state = ?, owners = ? WHERE id = ?",
                (json.dumps(state), json.dumps(owners), run_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    return result if result is not None else state


def set_message_ts(run_id: str, message_ts: str) -> None:
    with _lock, _connect() as conn:
        conn.execute("UPDATE runs SET message_ts = ? WHERE id = ?", (message_ts, run_id))


def update_record(
    run_id: str, record: Dict[str, Any], project_name: str, project_type: str, user_id: str
) -> List[str]:
    """Replace a run's answers. Returns a human-readable list of what changed.

    The checklist, its ticks and its sign-offs are untouched: correcting a date
    or a customer name says nothing about whether the seed set was any good.
    """
    labels = _record_labels()
    changes: List[str] = []

    with _lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT record, state FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                raise RunGone(run_id)

            before = json.loads(row["record"] or "{}")
            for key in sorted(set(before) | set(record)):
                if key == TYPE_FIELD:
                    continue
                old, new = before.get(key), record.get(key)
                if old == new:
                    continue
                changes.append(
                    f"*{labels.get(key, key)}*: {_shown(old)} → {_shown(new)}"
                )

            if not changes:
                conn.rollback()
                return []

            state = json.loads(row["state"])
            state.setdefault("edits", []).append(
                {"by": user_id, "at": _now(), "changes": changes}
            )
            conn.execute(
                "UPDATE runs SET record = ?, project_name = ?, project_type = ?, state = ?"
                " WHERE id = ?",
                (json.dumps(record), project_name, project_type, json.dumps(state), run_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    return changes


TYPE_FIELD = "project_type"


def _record_labels() -> Dict[str, str]:
    try:
        from form_builder import load_schema

        schema = load_schema()
        fields = list(schema.get("common_fields", []))
        for defn in schema.get("project_types", {}).values():
            fields.extend(defn.get("fields", []))
        return {f["key"]: f["label"] for f in fields if f.get("type") != "context"}
    except Exception:
        return {}


def _shown(value: Any) -> str:
    if value in (None, "", []):
        return "_empty_"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    text = str(value)
    return text if len(text) <= 60 else text[:59] + "…"


def open_ingestions() -> List[Dict[str, Any]]:
    """Runs still waiting on a go/no-go, for the 36-hour reminder sweep."""
    import ingestion

    with _lock, _connect() as conn:
        rows = conn.execute("SELECT id FROM runs").fetchall()
    runs = [get_run(r["id"]) for r in rows]
    return [r for r in runs if r and ingestion.awaiting_decision(r["state"])]


def set_doc(run_id: str, doc_id: str, doc_url: str) -> None:
    """Remember the project's collaboration doc."""
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE runs SET doc_id = ?, doc_url = ? WHERE id = ?", (doc_id, doc_url, run_id)
        )


def set_thread(run_id: str, channel: str, thread_ts: str) -> None:
    """Attach a run to its channel message.

    The summary post needs the run id to build its 'View full intake' button, so
    the run has to exist before the message does — and the message timestamp only
    exists afterwards.
    """
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE runs SET channel = ?, thread_ts = ? WHERE id = ?",
            (channel, thread_ts, run_id),
        )


# --------------------------------------------------------------------------
# gate logic
# --------------------------------------------------------------------------

def step_complete(step: Dict[str, Any], state: Dict[str, Any]) -> bool:
    checked = set(state["checked"].get(step["key"], []))
    if not {i["key"] for i in step["items"]} <= checked:
        return False
    for owner_key in step.get("signoffs", []):
        if not state["signoffs"].get(f"{step['key']}:{owner_key}"):
            return False
    return True


def current_step(state: Dict[str, Any]) -> Dict[str, Any]:
    """The earliest step still outstanding — what the run is 'on' for reporting."""
    for step in STEPS:
        if not step_complete(step, state):
            return step
    return STEPS[-1]


def is_locked(step: Dict[str, Any], state: Dict[str, Any]) -> bool:
    """Only the checklist's one explicit ordering rule locks a step.

    Everything else may be worked in parallel: the audit request gets drafted
    while expansion is still running, reports get written while the audit is
    open. Blocking those would only teach people to tick boxes early.
    """
    blocker = step.get("blocked_by")
    return bool(blocker) and not step_complete(STEP_BY_KEY[blocker], state)


def locked_reason(step: Dict[str, Any]) -> str:
    blocker = STEP_BY_KEY[step["blocked_by"]]
    return f"Locked until Step {blocker['number']} passes — never expand past an unvetted seed set."


def audit_hours_elapsed(state: Dict[str, Any]) -> Optional[float]:
    stamp = state.get("audit_posted_at")
    if not stamp:
        return None
    posted = dt.datetime.fromisoformat(stamp)
    return (dt.datetime.now(dt.timezone.utc) - posted).total_seconds() / 3600


def ship_blockers(state: Dict[str, Any]) -> List[str]:
    """Every reason this run may not ship yet, in the checklist's own terms."""
    import ingestion

    blockers: List[str] = []
    if not ingestion.complete(state):
        blockers.append("Opportunity ingestion not signed off")

    for step in STEPS:
        checked = set(state["checked"].get(step["key"], []))
        missing = [i for i in step["items"] if i["key"] not in checked]
        if missing:
            blockers.append(f"Step {step['number']}: {len(missing)} item(s) unchecked")
        for owner_key in step.get("signoffs", []):
            if not state["signoffs"].get(f"{step['key']}:{owner_key}"):
                blockers.append(
                    f"Step {step['number']}: {SIGNOFF_LABELS[owner_key]} sign-off missing"
                )

    elapsed = audit_hours_elapsed(state)
    if elapsed is None:
        blockers.append("Audit request not posted yet")
    elif elapsed < AUDIT_MIN_HOURS:
        left = AUDIT_MIN_HOURS - elapsed
        blockers.append(f"Audit needs {left:.1f}h more — never the same day")

    return blockers


def can_ship(state: Dict[str, Any]) -> bool:
    return not ship_blockers(state)


def status_line(state: Dict[str, Any]) -> str:
    """One line fit for a spreadsheet cell: where this run actually stands."""
    closed = state.get("closed")
    if closed:
        return f"Closed — {closed['reason']}"

    # Nothing downstream means much until Delivery has accepted the work.
    import ingestion  # imported here: ingestion builds on this module

    pending = ingestion.status(state)
    if pending:
        return pending

    if can_ship(state):
        return "Cleared to ship"

    current = current_step(state)
    checked = len(state["checked"].get(current["key"], []))
    total = len(current["items"])
    line = f"Step {current['number']} · {current['title']} ({checked}/{total})"

    pending = [
        SIGNOFF_LABELS[o]
        for o in current.get("signoffs", [])
        if not state["signoffs"].get(f"{current['key']}:{o}")
    ]
    if checked == total and pending:
        line += f" — awaiting {' and '.join(pending)} sign-off"

    if current["key"] == "ship":
        elapsed = audit_hours_elapsed(state)
        if elapsed is not None and elapsed < AUDIT_MIN_HOURS:
            line += f" — audit window has {AUDIT_MIN_HOURS - elapsed:.1f}h left"

    returned = len(state.get("returned") or [])
    if returned:
        line += f" · returned to taxonomy {returned}×"

    return line


# --------------------------------------------------------------------------
# mutations
# --------------------------------------------------------------------------

def set_checked(run_id: str, step_key: str, item_keys: List[str]) -> Tuple[bool, str, Dict[str, Any]]:
    """Replace a step's ticks with whatever the checkbox group now reports.

    Refuses on a closed or locked step. The rendered message already hides those
    controls, but a client holding a stale copy of the message can still send the
    action, and a tick that lands on a re-locked step would count towards ship
    forever.
    """
    step = STEP_BY_KEY[step_key]
    refusal = {}

    def change(state, owners):
        if state.get("closed"):
            refusal["message"] = "This project is closed. Reopen it before changing anything."
            return state
        if is_locked(step, state):
            refusal["message"] = locked_reason(step)
            return state

        was = set(state["checked"].get(step_key, []))
        now = set(item_keys)
        state["checked"][step_key] = item_keys

        # The audit clock tracks the request that is currently up. It starts when
        # the item goes from unticked to ticked, and clears when it is unticked —
        # otherwise a stray early tick banks the 24 hours, and the real request
        # weeks later can ship the same day, which is the one thing the rule
        # forbids. Clearing is the safe direction: an unticked item blocks ship
        # by itself, so nobody loses time they had legitimately earned.
        for item in step["items"]:
            if not item.get("starts_clock"):
                continue
            if item["key"] in now and item["key"] not in was:
                state["audit_posted_at"] = _now()
            elif item["key"] not in now:
                state["audit_posted_at"] = None

        # Changing an item invalidates sign-offs on that step: people signed off
        # on what they read, not on whatever it becomes afterwards.
        for owner_key in step.get("signoffs", []):
            state["signoffs"].pop(f"{step_key}:{owner_key}", None)
        return state

    state = _mutate(run_id, change)
    if refusal:
        return False, refusal["message"], state
    return True, "", state


def sign_off(run_id: str, step_key: str, owner_key: str, user_id: str) -> Tuple[bool, str]:
    """Record a sign-off. Only the person currently named in that role may give it."""
    step = STEP_BY_KEY[step_key]
    outcome = {}

    def change(state, owners):
        if state.get("closed"):
            outcome["message"] = "This project is closed. Reopen it before signing off."
            return state

        expected = owners.get(owner_key)
        if expected and user_id != expected:
            outcome["message"] = (
                f"Only <@{expected}> can give the {SIGNOFF_LABELS[owner_key]} sign-off "
                f"on this run. Use *Change owners* if the role has moved."
            )
            return state

        checked = set(state["checked"].get(step_key, []))
        if not {i["key"] for i in step["items"]} <= checked:
            outcome["message"] = f"Tick every item in Step {step['number']} before signing off."
            return state

        state["signoffs"][f"{step_key}:{owner_key}"] = {"user": user_id, "at": _now()}
        outcome["ok"] = True
        outcome["message"] = (
            f"{SIGNOFF_LABELS[owner_key]} sign-off recorded for Step {step['number']}."
        )
        return state

    _mutate(run_id, change)
    return bool(outcome.get("ok")), outcome["message"]


CLOSE_REASONS = [
    ("delivered", "Delivered — shipped to the customer"),
    ("no_go", "No go — we decided not to do it"),
    ("customer_dropped", "Customer dropped it"),
    ("superseded", "Superseded by another project"),
    ("stalled", "Stalled — parked indefinitely"),
]
CLOSE_REASON_LABELS = dict(CLOSE_REASONS)


def close_run(run_id: str, user_id: str, reason_key: str, note: str = "") -> Dict[str, Any]:
    """Close a run. Nothing is deleted — the record and its ticks stay readable."""

    def change(state, owners):
        state["closed"] = {
            "by": user_id,
            "at": _now(),
            "reason_key": reason_key,
            "reason": CLOSE_REASON_LABELS.get(reason_key, reason_key),
            "note": note,
        }
        return state

    return _mutate(run_id, change)


def reopen_run(run_id: str, user_id: str) -> Dict[str, Any]:
    """Reopen a closed run with every tick and sign-off intact."""

    def change(state, owners):
        closed = state.pop("closed", None)
        state.setdefault("reopened", []).append(
            {"by": user_id, "at": _now(), "was": closed.get("reason") if closed else None}
        )
        return state

    return _mutate(run_id, change)


def set_owners(
    run_id: str, new_owners: Dict[str, str], user_id: str
) -> Tuple[Dict[str, str], List[str], str]:
    """Replace the named owners. Returns the owners, what changed, and any warning.

    Sign-offs already given are kept: they were valid when they were given, and
    a handover does not un-review the work. Sign-offs still outstanding now
    belong to whoever holds the role from here on.
    """
    outcome: Dict[str, Any] = {"changes": [], "warning": ""}

    def change(state, owners):
        changes = [
            f"{SIGNOFF_LABELS[k]}: "
            + (f"<@{owners[k]}> → <@{v}>" if owners.get(k) else f"set to <@{v}>")
            for k, v in new_owners.items()
            if owners.get(k) != v
        ]
        outcome["changes"] = changes
        if not changes:
            outcome["owners"] = dict(owners)
            return state

        # One person holding both roles turns a joint gate into a solo one. That
        # is nearly always a mis-click rather than a decision, so it is allowed
        # but called out where the team can see it.
        if new_owners.get("research_owner") and (
            new_owners.get("research_owner") == new_owners.get("delivery_owner")
        ):
            outcome["warning"] = (
                "Research and Delivery are now the same person, so the alignment and "
                "audit gates are no longer a joint sign-off."
            )

        owners.clear()
        owners.update(new_owners)
        state.setdefault("owner_changes", []).append(
            {"by": user_id, "at": _now(), "changes": changes}
        )
        outcome["owners"] = dict(owners)
        return state

    _mutate(run_id, change)
    return outcome["owners"], outcome["changes"], outcome["warning"]


def return_to_taxonomy(run_id: str, user_id: str) -> Dict[str, Any]:
    """No-go at the alignment gate: everything downstream describes discarded work.

    Clearing only Steps 2 and 3 leaves the expand, audit and ship ticks — and the
    audit clock — describing the sample set that was just rejected. Re-ticking the
    two cleared steps would then clear the run to ship with no new QC, no new
    audit request and no fresh 24-hour window. That is exactly the "do not patch
    the samples and continue" this is here to prevent, so the whole run downstream
    of the gate goes back with it. The old values are kept in the returned entry
    rather than dropped, so nothing disappears without a record.
    """

    def change(state, owners):
        state.setdefault("returned", []).append(
            {
                "by": user_id,
                "at": _now(),
                "cleared": {
                    "checked": {k: list(v) for k, v in state["checked"].items()},
                    "signoffs": sorted(state["signoffs"]),
                    "audit_posted_at": state.get("audit_posted_at"),
                },
            }
        )
        for step in STEPS:
            state["checked"][step["key"]] = []
        state["signoffs"] = {}
        state["audit_posted_at"] = None
        return state

    return _mutate(run_id, change)
