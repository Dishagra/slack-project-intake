"""Drives a full checklist run through Steps 2-6 against a temp database.

Run: python test_gates.py
"""

import datetime as dt
import json
import os
import tempfile

os.environ["GATES_DB"] = os.path.join(tempfile.mkdtemp(), "gates_test.db")

import gates  # noqa: E402  — must follow the env override
from gate_blocks import checklist_blocks  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail and not cond else ''}")
    if not cond:
        failures.append(name)


RESEARCH, DELIVERY, SALES, OUTSIDER = "U_RES", "U_DEL", "U_SAL", "U_RANDOM"

gates.init_db()
run_id = gates.create_run(
    "C123", "1700000000.1", "Multi-Turn Attack Bench", "sample",
    {"research_owner": RESEARCH, "delivery_owner": DELIVERY, "sales_owner": SALES},
)
run = gates.get_run(run_id)
check("run persists", run is not None and run["project_name"] == "Multi-Turn Attack Bench")
check("owners persist", run["owners"]["research_owner"] == RESEARCH)


def state():
    return gates.get_run(run_id)["state"]


def tick(step_key, keys, rid=None):
    ok, refusal, state = gates.set_checked(rid or run_id, step_key, keys)
    return state


def all_items(step_key):
    return [i["key"] for i in gates.STEP_BY_KEY[step_key]["items"]]


def _numeric(text):
    """Would Sheets read this as a number rather than text?

    Covers plain digits and the scientific notation a hex string can fall into
    ("12e34"), which is the shape that silently changes value on the way in.
    """
    try:
        float(text)
        return True
    except ValueError:
        return False


# --- everything downstream starts locked --------------------------------
s = state()
check("step 2 open at start", not gates.is_locked(gates.STEP_BY_KEY["seed"], s))
check("step 3 open at start", not gates.is_locked(gates.STEP_BY_KEY["alignment"], s))
check("step 4 locked at start", gates.is_locked(gates.STEP_BY_KEY["expand"], s))
check("ship blocked at start", not gates.can_ship(s))

# Parallel work: everything except Expand is available from the outset, because
# the checklist only forbids expanding past an unvetted seed set.
for key in ("seed", "alignment", "audit", "ship"):
    check(f"{key} is workable in parallel", not gates.is_locked(gates.STEP_BY_KEY[key], s))
check("expand is the only locked step",
      [st["key"] for st in gates.STEPS if gates.is_locked(st, s)] == ["expand"])
check("audit can be prepped early", not gates.is_locked(gates.STEP_BY_KEY["audit"], s))

# Ticking a later step early does not let it ship
tick("ship", all_items("ship"))
check("early ship ticks do not clear ship", not gates.can_ship(state()))
tick("ship", [])

# --- Step 2: seed set ---------------------------------------------------
s = tick("seed", ["fidelity"])
check("partial seed tick keeps expand locked", gates.is_locked(gates.STEP_BY_KEY["expand"], s))
s = tick("seed", all_items("seed"))
check("step 2 completes", gates.step_complete(gates.STEP_BY_KEY["seed"], s))
check("step 4 still locked", gates.is_locked(gates.STEP_BY_KEY["expand"], s))

# --- Step 3: alignment gate, sign-off enforcement -----------------------
ok, msg = gates.sign_off(run_id, "alignment", "research_owner", RESEARCH)
check("cannot sign off before items ticked", not ok, msg)

tick("alignment", all_items("alignment"))
ok, msg = gates.sign_off(run_id, "alignment", "research_owner", OUTSIDER)
check("outsider cannot sign off", not ok)
check("refusal names the right person", RESEARCH in msg, msg)

ok, msg = gates.sign_off(run_id, "alignment", "research_owner", DELIVERY)
check("delivery cannot give research sign-off", not ok)

ok, _ = gates.sign_off(run_id, "alignment", "research_owner", RESEARCH)
check("research signs off", ok)
check("one sign-off is not enough", gates.is_locked(gates.STEP_BY_KEY["expand"], state()))

ok, _ = gates.sign_off(run_id, "alignment", "delivery_owner", DELIVERY)
check("delivery signs off", ok)
check("step 4 unlocks after both sign-offs", not gates.is_locked(gates.STEP_BY_KEY["expand"], state()))

# --- editing a signed-off step invalidates the sign-offs ----------------
s = tick("alignment", ["quality_bar"])
check("re-editing clears sign-offs", not s["signoffs"].get("alignment:research_owner"))
check("step 4 re-locks", gates.is_locked(gates.STEP_BY_KEY["expand"], s))

tick("alignment", all_items("alignment"))
gates.sign_off(run_id, "alignment", "research_owner", RESEARCH)
gates.sign_off(run_id, "alignment", "delivery_owner", DELIVERY)
check("step 4 unlocks again", not gates.is_locked(gates.STEP_BY_KEY["expand"], state()))

# --- no-go returns to taxonomy -----------------------------------------
# A no-go discards the sample set, so everything describing it goes too.
# Leaving the expand/audit/ship ticks and the audit clock in place let a run
# re-clear to ship in seven clicks with no new QC and no new audit window.
tick("expand", all_items("expand"))
tick("audit", all_items("audit"))
gates.sign_off(run_id, "audit", "research_owner", RESEARCH)
gates.sign_off(run_id, "audit", "delivery_owner", DELIVERY)
tick("ship", all_items("ship"))
check("run is fully ticked before the no-go",
      all(gates.step_complete(st, state()) for st in gates.STEPS))

gates.return_to_taxonomy(run_id, RESEARCH)
s = state()
for step in gates.STEPS:
    check(f"no-go clears {step['key']}", s["checked"][step["key"]] == [],
          str(s["checked"][step["key"]]))
check("no-go drops every sign-off", s["signoffs"] == {}, str(s["signoffs"]))
check("no-go stops the audit clock", s["audit_posted_at"] is None)
check("no-go re-locks expand", gates.is_locked(gates.STEP_BY_KEY["expand"], s))
check("no-go is recorded", len(s["returned"]) == 1 and s["returned"][0]["by"] == RESEARCH)
check("no-go keeps what it cleared", s["returned"][0]["cleared"]["checked"]["ship"] != [])
check("ship is blocked again", not gates.can_ship(s))

# re-ticking only steps 2 and 3 must NOT clear ship again
tick("seed", all_items("seed"))
tick("alignment", all_items("alignment"))
gates.sign_off(run_id, "alignment", "research_owner", RESEARCH)
gates.sign_off(run_id, "alignment", "delivery_owner", DELIVERY)
check("re-passing the gate alone does not clear ship", not gates.can_ship(state()),
      str(gates.ship_blockers(state()))[:120])

# rebuild past the gate
tick("expand", all_items("expand"))
check("step 5 stays open", not gates.is_locked(gates.STEP_BY_KEY["audit"], state()))

# --- Step 5: the audit clock -------------------------------------------
check("clock not running before the request is posted", gates.audit_hours_elapsed(state()) is None)
s = tick("audit", ["posted"])
check("posting the request starts the clock", s["audit_posted_at"] is not None)
first_stamp = s["audit_posted_at"]

# Un-ticking clears the clock, and re-ticking starts a fresh 24h. The old
# behaviour banked the window: one stray early tick, unticked, still counted
# weeks later - so the real request could ship the same day it went up.
s = tick("audit", [])
check("un-ticking clears the clock", s["audit_posted_at"] is None)
s = tick("audit", all_items("audit"))
check("re-ticking restarts the clock", s["audit_posted_at"] is not None)
check("restarted clock is recent",
      (dt.datetime.now(dt.timezone.utc)
       - dt.datetime.fromisoformat(s["audit_posted_at"])).total_seconds() < 5)

gates.sign_off(run_id, "audit", "research_owner", RESEARCH)
gates.sign_off(run_id, "audit", "delivery_owner", DELIVERY)
tick("ship", all_items("ship"))

s = state()
blockers = gates.ship_blockers(s)
check("all steps complete", all(gates.step_complete(st, s) for st in gates.STEPS))
check("ship still blocked by the 24h window", not gates.can_ship(s))
check("blocker names the same-day rule", any("same day" in b for b in blockers), str(blockers))

# wind the clock back past 24h
s["audit_posted_at"] = (
    dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)
).isoformat(timespec="seconds")
gates.save_state(run_id, s)
s = state()
check("ship clears after 24h", gates.can_ship(s), str(gates.ship_blockers(s)))

# --- rendering ----------------------------------------------------------
run = gates.get_run(run_id)
blocks = checklist_blocks(run)
json.dumps(blocks)
check("checklist renders", len(blocks) > 10)
check("checklist under 50 blocks", len(blocks) <= 50, str(len(blocks)))
text = json.dumps(blocks)
check("cleared-to-ship banner shows", "Cleared to ship" in text)

# a fresh run renders its locked steps without controls
fresh = gates.get_run(
    gates.create_run("C123", "1700000000.2", "Fresh run", "sample", {"research_owner": RESEARCH})
)
fresh_blocks = checklist_blocks(fresh)
json.dumps(fresh_blocks)
action_ids = [
    e.get("action_id", "")
    for b in fresh_blocks
    if b["type"] == "actions"
    for e in b["elements"]
]
check("fresh run renders", len(fresh_blocks) > 10)
check("expand has no controls while locked",
      not any(":expand" in a for a in action_ids), str(action_ids))
check("other steps are interactive",
      all(k in " ".join(action_ids) for k in (":seed", ":alignment", ":audit", ":ship")))
check("locked steps say so", "Locked until" in json.dumps(fresh_blocks))
check("not-cleared banner shows", "Not cleared to ship" in json.dumps(fresh_blocks))

# --- owners change mid-flight -------------------------------------------
NEW_DELIVERY = "U_DEL2"
owners, changes, warning = gates.set_owners(
    run_id,
    {"research_owner": RESEARCH, "delivery_owner": NEW_DELIVERY, "sales_owner": SALES},
    RESEARCH,
)
check("owner change reported", len(changes) == 1, str(changes))
check("change names both people", DELIVERY in changes[0] and NEW_DELIVERY in changes[0])
check("new owner persists", gates.get_run(run_id)["owners"]["delivery_owner"] == NEW_DELIVERY)
check("earlier sign-offs survive a handover",
      gates.get_run(run_id)["state"]["signoffs"].get("alignment:delivery_owner"))
check("handover is recorded", len(gates.get_run(run_id)["state"]["owner_changes"]) == 1)
check("no-op change reports nothing", gates.set_owners(run_id, owners, RESEARCH)[1] == [])
check("no warning for distinct owners", warning == "", warning)

# the new owner is the one who can sign off from here on
fresh_id = gates.create_run("C1", "1.9", "Handover test", "sample",
                            {"research_owner": RESEARCH, "delivery_owner": DELIVERY})
gates.set_checked(fresh_id, "alignment", all_items("alignment"))
gates.set_owners(fresh_id, {"research_owner": RESEARCH, "delivery_owner": NEW_DELIVERY}, RESEARCH)
ok, msg = gates.sign_off(fresh_id, "alignment", "delivery_owner", DELIVERY)
check("previous owner can no longer sign off", not ok, msg)
ok, _ = gates.sign_off(fresh_id, "alignment", "delivery_owner", NEW_DELIVERY)
check("new owner can sign off", ok)

# --- closing and reopening ----------------------------------------------
s = gates.close_run(run_id, DELIVERY, "customer_dropped", "Budget pulled in Q3.")
check("close is recorded", s["closed"]["reason_key"] == "customer_dropped")
check("close keeps the note", s["closed"]["note"] == "Budget pulled in Q3.")
check("closed status line", gates.status_line(s).startswith("Closed —"), gates.status_line(s))
check("closing does not erase ticks", s["checked"]["seed"] == all_items("seed"))
check("closing does not erase sign-offs", s["signoffs"].get("alignment:research_owner"))

closed_blocks = checklist_blocks(gates.get_run(run_id))
json.dumps(closed_blocks)
closed_text = json.dumps(closed_blocks)
closed_actions = [
    e.get("action_id", "")
    for b in closed_blocks
    if b["type"] == "actions"
    for e in b["elements"]
]
check("closed run shows why", "Customer dropped it" in closed_text)
check("closed run offers reopen", any("gate_reopen" in a for a in closed_actions))
check("closed run has no tick controls", not any("gate_check" in a for a in closed_actions))
check("closed run has no sign-off controls", not any("gate_signoff" in a for a in closed_actions))
check("closed run still shows progress", "Step 2" in closed_text)

s = gates.reopen_run(run_id, RESEARCH)
check("reopen clears the closed flag", "closed" not in s)
check("reopen is recorded", s["reopened"][0]["by"] == RESEARCH)
check("reopen keeps every tick", s["checked"]["seed"] == all_items("seed"))
check("reopened run can ship again", gates.can_ship(s), str(gates.ship_blockers(s)))
reopened_actions = json.dumps(checklist_blocks(gates.get_run(run_id)))
check("reopened run is interactive again", "gate_check" in reopened_actions)

# --- writes are refused, not just hidden --------------------------------
# The rendered message hides controls for locked and closed steps, but a client
# holding a stale copy can still send the action. A tick that lands on a
# re-locked step would count towards ship forever.
guard_id = gates.create_run("C1", "1.6", "Guard test", "sample",
                            {"research_owner": RESEARCH, "delivery_owner": DELIVERY})
ok, refusal, _ = gates.set_checked(guard_id, "expand", all_items("expand"))
check("locked step refuses a write", not ok)
check("refusal explains the gate", "unvetted seed set" in refusal, refusal)
check("locked step stayed empty", gates.get_run(guard_id)["state"]["checked"].get("expand") in (None, []))

gates.close_run(guard_id, RESEARCH, "stalled")
ok, refusal, _ = gates.set_checked(guard_id, "seed", all_items("seed"))
check("closed run refuses a tick", not ok)
check("closed refusal says so", "closed" in refusal.lower(), refusal)
ok, msg = gates.sign_off(guard_id, "seed", "research_owner", RESEARCH)
check("closed run refuses a sign-off", not ok)
gates.reopen_run(guard_id, RESEARCH)
ok, _, _ = gates.set_checked(guard_id, "seed", all_items("seed"))
check("reopened run accepts writes again", ok)

# --- a missing run raises rather than crashing on None ------------------
try:
    gates.set_checked("run_doesnotexist", "seed", [])
    check("unknown run raises RunGone", False, "no exception")
except gates.RunGone:
    check("unknown run raises RunGone", True)
except Exception as exc:
    check("unknown run raises RunGone", False, type(exc).__name__)
check("get_run returns None for unknown", gates.get_run("run_doesnotexist") is None)

# --- concurrent mutations do not lose each other ------------------------
# Both mutators rewrite the whole state blob, so a read and a write that are
# separately locked can silently drop the other click's work.
import threading as _threading

race_id = gates.create_run("C1", "1.7", "Race test", "sample", {"research_owner": RESEARCH})
errors = []


def hammer(step_key, items):
    try:
        for _ in range(25):
            gates.set_checked(race_id, step_key, items)
            gates.set_checked(race_id, step_key, [])
            gates.set_checked(race_id, step_key, items)
    except Exception as exc:  # pragma: no cover - only fires on a real defect
        errors.append(exc)


threads = [
    _threading.Thread(target=hammer, args=("seed", all_items("seed"))),
    _threading.Thread(target=hammer, args=("audit", all_items("audit"))),
    _threading.Thread(target=hammer, args=("ship", all_items("ship"))),
]
for th in threads:
    th.start()
for th in threads:
    th.join()

final = gates.get_run(race_id)["state"]
check("concurrent mutations raised nothing", not errors, str(errors[:1]))
check("all three steps survived the race",
      all(final["checked"].get(k) for k in ("seed", "audit", "ship")),
      str({k: final["checked"].get(k) for k in ("seed", "audit", "ship")}))

# --- run ids are never all-digits --------------------------------------
# Sheets coerces a numeric-looking id, so the row could never be found again.
ids = [gates.create_run("C1", "1.8", "id test", "sample", {}) for _ in range(200)]
check("run ids are prefixed", all(i.startswith("run_") for i in ids))
check("no generated id can be read as a number", not any(_numeric(i) for i in ids))

# The hex suffix is all-digits (or "12e34"-shaped) about 1 in 137 times, which is
# the whole reason for the prefix. Assert that on the shapes themselves rather
# than by sampling — a probabilistic check would fail a few runs in every hundred
# for no reason, and a test that cries wolf gets ignored.
for risky in ("012345678901", "123456789012", "12e345678901"):
    check(f"bare {risky!r} would have been coerced", _numeric(risky))
    check(f"prefixed {risky!r} is safe", not _numeric("run_" + risky))

# --- the record travels with the run ------------------------------------
rec_id = gates.create_run("C1", "1.5", "With record", "sample", {}, record={"account": "Tencent"})
check("record persists", gates.get_run(rec_id)["record"]["account"] == "Tencent")
check("missing record reads as empty", gates.get_run(fresh_id)["record"] == {})

print()
print(f"{len(failures)} failure(s)" if failures else "all checks passed")
raise SystemExit(1 if failures else 0)
