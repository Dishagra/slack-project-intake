"""The Block Kit for the gate-tracking thread message.

One message, rewritten in place on every interaction. Locked steps render as
plain text with no controls, so an unreachable gate cannot be ticked by mistake.
"""

from __future__ import annotations

from typing import Any, Dict, List

from admin_views import clamp
from gates import (
    AUDIT_IDEAL_HOURS,
    AUDIT_MIN_HOURS,
    SIGNOFF_LABELS,
    STEPS,
    audit_hours_elapsed,
    can_ship,
    is_locked,
    locked_reason,
    ship_blockers,
    step_complete,
)

CHECK_ACTION = "gate_check"
SIGNOFF_ACTION = "gate_signoff"
RETURN_ACTION = "gate_return"
CLOSE_ACTION = "gate_close"
REOPEN_ACTION = "gate_reopen"
OWNERS_ACTION = "gate_owners"


def _progress(state: Dict[str, Any]) -> str:
    done = sum(1 for s in STEPS if step_complete(s, state))
    filled = "▰" * done + "▱" * (len(STEPS) - done)
    return f"{filled}  {done}/{len(STEPS)} steps"


def _step_blocks(step: Dict[str, Any], state: Dict[str, Any], run_id: str,
                 read_only: bool = False) -> List[Dict[str, Any]]:
    if read_only:
        complete = step_complete(step, state)
        checked = set(state["checked"].get(step["key"], []))
        ticks = "\n".join(
            f"{'✓' if i['key'] in checked else '·'}  {i['text']}" for i in step["items"]
        )
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{'✅' if complete else '▫️'}  *Step {step['number']}. "
                    f"{step['title']}*\n{ticks}",
                },
            }
        ]

    locked = is_locked(step, state)
    complete = step_complete(step, state)
    checked = set(state["checked"].get(step["key"], []))

    mark = "✅" if complete else ("🔒" if locked else "▸")
    heading = f"{mark}  *Step {step['number']}. {step['title']}*"
    blocks: List[Dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": heading}},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"_{step['owner']}_"}],
        },
    ]

    if locked:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": locked_reason(step)}]}
        )
        return blocks

    if step.get("question"):
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f"*{step['question']}*"}]}
        )

    options = [
        {
            "text": {"type": "mrkdwn", "text": item["text"]},
            "value": item["key"],
        }
        for item in step["items"]
    ]
    element: Dict[str, Any] = {
        "type": "checkboxes",
        "action_id": f"{CHECK_ACTION}:{run_id}:{step['key']}",
        "options": options,
    }
    initial = [o for o in options if o["value"] in checked]
    if initial:
        element["initial_options"] = initial
    blocks.append({"type": "actions", "elements": [element]})

    # The audit clock, shown only once it is running.
    if step["key"] == "audit":
        elapsed = audit_hours_elapsed(state)
        if elapsed is not None:
            if elapsed >= AUDIT_IDEAL_HOURS:
                note = f"Audit has been open {elapsed:.0f}h. Past the {AUDIT_IDEAL_HOURS}h ideal."
            elif elapsed >= AUDIT_MIN_HOURS:
                note = (
                    f"Audit has been open {elapsed:.0f}h. Past the {AUDIT_MIN_HOURS}h minimum, "
                    f"under the {AUDIT_IDEAL_HOURS}h ideal."
                )
            else:
                note = (
                    f"⏳ Audit open {elapsed:.1f}h. Ship unlocks at {AUDIT_MIN_HOURS}h — "
                    "never the same day."
                )
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})

    buttons: List[Dict[str, Any]] = []
    for owner_key in step.get("signoffs", []):
        record = state["signoffs"].get(f"{step['key']}:{owner_key}")
        if record:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"✍️ {SIGNOFF_LABELS[owner_key]} signed off — <@{record['user']}>",
                        }
                    ],
                }
            )
        else:
            buttons.append(
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": f"{SIGNOFF_LABELS[owner_key]} sign-off"},
                    "action_id": f"{SIGNOFF_ACTION}:{run_id}:{step['key']}:{owner_key}",
                    "value": owner_key,
                }
            )

    # Available even once the gate has passed: finding out later that the
    # approach was wrong is exactly when this is needed, and hiding it forced
    # people to untick a passed gate just to reach its own control.
    if step.get("decision"):
        buttons.append(
            {
                "type": "button",
                "style": "danger",
                "text": {"type": "plain_text", "text": "No go — return to taxonomy"},
                "action_id": f"{RETURN_ACTION}:{run_id}",
                "value": "return",
                "confirm": {
                    "title": {"type": "plain_text", "text": "Return to taxonomy?"},
                    "text": {
                        "type": "mrkdwn",
                        "text": "This clears *every* step, both sign-offs and the audit clock — "
                        "all of it describes the sample set you are discarding. The checklist is "
                        "explicit: do not patch the samples and continue. Nothing is deleted; the "
                        "cleared values stay in the run's history.",
                    },
                    "confirm": {"type": "plain_text", "text": "Return to Step 1"},
                    "deny": {"type": "plain_text", "text": "Cancel"},
                },
            }
        )

    if buttons:
        blocks.append({"type": "actions", "elements": buttons})

    return blocks


def checklist_blocks(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    state = run["state"]
    owners = run["owners"]
    closed = state.get("closed")
    run_id = run["id"]

    owner_line = " · ".join(
        f"{SIGNOFF_LABELS[k]} <@{owners[k]}>" for k in SIGNOFF_LABELS if owners.get(k)
    )

    blocks: List[Dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": clamp(f"*Sample Creation Checklist* — {run['project_name']}"),
            },
        },
        {"type": "context", "elements": [{"type": "mrkdwn", "text": owner_line or "_No owners recorded_"}]},
    ]

    if closed:
        note = f"\n_{closed['note']}_" if closed.get("note") else ""
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*🔒 Closed — {closed['reason']}*\nby <@{closed['by']}>{note}",
                },
            }
        )
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Reopen"},
                        "action_id": f"{REOPEN_ACTION}:{run_id}",
                        "value": "reopen",
                    }
                ],
            }
        )
        blocks.append({"type": "divider"})
        for step in STEPS:
            blocks.extend(_step_blocks(step, state, run_id, read_only=True))
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": "_Nothing was deleted. Reopen to carry on._"}
                ],
            }
        )
        return blocks

    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"`{_progress(state)}`"}]})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "_Steps can run in parallel. Only Step 4 waits on the alignment "
                    "gate, and ship waits on the audit window._",
                }
            ],
        }
    )

    for record_key, icon, verb in (("returned", "↩️", "Returned to taxonomy"),
                                   ("owner_changes", "👥", "Owners changed"),
                                   ("reopened", "🔓", "Reopened")):
        entries = state.get(record_key) or []
        if entries:
            last = entries[-1]
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"{icon} {verb} by <@{last['by']}>"
                            + (f" — {len(entries)}×" if len(entries) > 1 else ""),
                        }
                    ],
                }
            )

    for step in STEPS:
        blocks.append({"type": "divider"})
        blocks.extend(_step_blocks(step, state, run_id))

    blocks.append({"type": "divider"})
    if can_ship(state):
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*🚢 Cleared to ship.* Every step complete, both reports ready, "
                    "audit had its full window.",
                },
            }
        )
    else:
        reasons = "\n".join(f"• {b}" for b in ship_blockers(state)[:8])
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Not cleared to ship*\n{reasons}"},
            }
        )
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "_If any answer is no, it does not ship._"}],
            }
        )

    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Change owners"},
                    "action_id": f"{OWNERS_ACTION}:{run_id}",
                    "value": "owners",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Close project"},
                    "action_id": f"{CLOSE_ACTION}:{run_id}",
                    "value": "close",
                },
            ],
        }
    )

    return blocks
