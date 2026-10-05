"""Opportunity Ingestion: the rules from Delivery's checklist, enforced.

Run: python test_ingestion.py
"""

import datetime as dt
import json
import os
import tempfile

os.environ["GATES_DB"] = os.path.join(tempfile.mkdtemp(), "ingest_test.db")

import gates  # noqa: E402
import ingestion  # noqa: E402
import ingestion_blocks  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail and not cond else ''}")
    if not cond:
        failures.append(name)


REQ, DEL, OTHER = "U_REQ", "U_DEL", "U_OTHER"
ALL = ingestion.ELEMENT_KEYS
RECORD = {"project_name": "CUA dataset", "ingestion_kind": "new",
          "spec_doc": "https://docs.google.com/document/d/spec", "task_volume": "500–800",
          "tat_agreed": "3 weeks", "milestones": "Weekly output numbers"}

gates.init_db()


def new_run(record=RECORD):
    rid = gates.create_run("C1", "1.1", record["project_name"], "pilot",
                           {"requestor": REQ, "delivery_owner": DEL}, record=record)
    if ingestion.needed(record):
        ingestion.start(rid, record)
    return rid


def state(rid):
    return gates.get_run(rid)["state"]


# --- who needs it ---------------------------------------------------------
check("new opportunities need ingestion", ingestion.needed({"ingestion_kind": "new"}))
check("recurring accounts skip it", not ingestion.needed({"ingestion_kind": "recurring"}))
check("unspecified defaults to needing it", ingestion.needed({}))
check("a run without ingestion counts as complete", ingestion.complete({"checked": {}}))

rid = new_run()
s = state(rid)
check("ingestion starts open", not ingestion.complete(s))
check("the clock starts at submission", ingestion.hours_elapsed(s) < 0.01)
check("deadline is 36 hours out",
      abs((ingestion.deadline(s) - dt.datetime.fromisoformat(s["ingestion"]["requested_at"]))
          - dt.timedelta(hours=36)) < dt.timedelta(seconds=1))

# --- only the Delivery owner signs elements and decides -------------------
ok, msg = ingestion.set_signed(rid, ALL, OTHER)
check("an outsider cannot sign off elements", not ok)
check("the refusal names the Delivery owner", DEL in msg, msg)
ok, msg = ingestion.set_signed(rid, ALL, REQ)
check("the requestor cannot sign off their own elements", not ok)

ok, _ = ingestion.set_signed(rid, ["spec_doc", "task_volume"], DEL)
check("the Delivery owner signs elements", ok)
signed = state(rid)["ingestion"]["signed"]
check("each signed element carries a date", all("at" in v for v in signed.values()))
first_stamp = signed["spec_doc"]["at"]

ingestion.set_signed(rid, ["spec_doc", "task_volume", "tat_agreed"], DEL)
check("re-ticking keeps the original sign-off date",
      state(rid)["ingestion"]["signed"]["spec_doc"]["at"] == first_stamp)

ok, msg = ingestion.decide(rid, "go", DEL)
check("go needs every element signed", not ok)
check("the refusal lists what is missing", "Progress milestones" in msg, msg)

ok, msg = ingestion.decide(rid, "go", OTHER)
check("an outsider cannot call go", not ok)

# --- an edit clears sign-offs on what changed ------------------------------
cleared = ingestion.invalidate(rid, ["spec_doc", "crucial_details"])
ingestion.sync_auto(rid, RECORD)  # the app always re-applies the blank rule after an edit
check("editing a signed element clears it", "spec_doc" in cleared, str(cleared))
check("a still-blank optional element stays settled",
      ingestion.is_auto(state(rid)["ingestion"], "crucial_details"))
check("other sign-offs survive the edit", "task_volume" in state(rid)["ingestion"]["signed"])

# --- go, then the final sign-off pair --------------------------------------
ingestion.set_signed(rid, ALL, DEL)
ok, msg, _ = ingestion.sign_off(rid, "requestor", REQ)
check("final sign-off waits for go", not ok, msg)

ok, _ = ingestion.decide(rid, "go", DEL)
check("go once everything is signed", ok)
ok, msg = ingestion.decide(rid, "no_go", DEL, "changed my mind")
check("the call cannot be made twice", not ok)

ok, msg = ingestion.set_signed(rid, [], DEL)
check("elements lock once go is called", not ok)
check("invalidate does nothing after go", ingestion.invalidate(rid, ["spec_doc"]) == [])

ok, msg, done = ingestion.sign_off(rid, "requestor", OTHER)
check("only the named requestor signs as requestor", not ok and REQ in msg, msg)
ok, msg, done = ingestion.sign_off(rid, "requestor", REQ)
check("requestor signs", ok and not done)
check("one signature is not enough", not ingestion.complete(state(rid)))
ok, msg, done = ingestion.sign_off(rid, "delivery_owner", DEL)
check("both signatures complete ingestion", ok and done)
check("complete() agrees", ingestion.complete(state(rid)))

# --- blank optional elements need no sign-off --------------------------------
auto_run = new_run()  # RECORD leaves example_task and crucial_details blank
ing = state(auto_run)["ingestion"]
check("blank optional elements are settled at submission",
      ingestion.is_auto(ing, "example_task") and ingestion.is_auto(ing, "crucial_details"))
check("required elements are not", not any(k in ing["signed"] for k in
      ("spec_doc", "task_volume", "tat_agreed", "milestones")))
check("auto entries are not recorded as Delivery's sign-off",
      ing["signed"]["example_task"]["by"] == "auto")

ok, _ = ingestion.set_signed(auto_run, ["spec_doc", "task_volume", "tat_agreed", "milestones"], DEL)
ok, msg = ingestion.decide(auto_run, "go", DEL)
check("go needs only the elements that were provided", ok, msg)

untick = new_run()
ingestion.set_signed(untick, [], DEL)
check("unticking everything leaves the automatic ones alone",
      ingestion.is_auto(state(untick)["ingestion"], "example_task"))

filled = dict(RECORD, example_task="https://x.test/example")
ingestion.sync_auto(untick, filled)
check("filling an optional element means Delivery must check it",
      "example_task" not in state(untick)["ingestion"]["signed"])
ingestion.sync_auto(untick, RECORD)
check("emptying it again settles it again", ingestion.is_auto(state(untick)["ingestion"], "example_task"))

opts = [o["value"] for b in ingestion_blocks.blocks(gates.get_run(untick))
        if b["type"] == "actions" for e in b["elements"] if e.get("type") == "checkboxes"
        for o in e["options"]]
check("blank optional elements are not offered for ticking",
      "example_task" not in opts and "crucial_details" not in opts, str(opts))
check("provided elements are", "spec_doc" in opts and "milestones" in opts)
check("blank ones read as not provided",
      "optional, not provided" in json.dumps(ingestion_blocks.blocks(gates.get_run(untick))))

# --- no-go needs a reason ---------------------------------------------------
rid2 = new_run()
ok, msg = ingestion.decide(rid2, "no_go", DEL, "   ")
check("a no-go without a reason is refused", not ok)
ok, _ = ingestion.decide(rid2, "no_go", DEL, "No acceptance criteria in the spec.")
check("a no-go with a reason is recorded", ok)

# --- the 36-hour clock ------------------------------------------------------
rid3 = new_run()
start = dt.datetime.fromisoformat(state(rid3)["ingestion"]["requested_at"])
check("no nudge at 10h", ingestion.due_nudges(state(rid3), start + dt.timedelta(hours=10)) == [])
check("warning at 31h", ingestion.due_nudges(state(rid3), start + dt.timedelta(hours=31)) == ["warn"])
ingestion.record_nudge(rid3, "warn")
check("warning is sent once", ingestion.due_nudges(state(rid3), start + dt.timedelta(hours=33)) == [])
check("breach at 37h", ingestion.due_nudges(state(rid3), start + dt.timedelta(hours=37)) == ["breach"])
ingestion.record_nudge(rid3, "breach")
check("breach is sent once", ingestion.due_nudges(state(rid3), start + dt.timedelta(hours=50)) == [])

late = state(rid3)
check("status says overdue",
      "overdue" in ingestion.status(late, start + dt.timedelta(hours=40)),
      ingestion.status(late, start + dt.timedelta(hours=40)))
check("status counts down before the deadline",
      "due in 26h" in ingestion.status(late, start + dt.timedelta(hours=10)),
      ingestion.status(late, start + dt.timedelta(hours=10)))

check("decided runs get no nudges", ingestion.due_nudges(state(rid2), start + dt.timedelta(hours=99)) == [])
check("open_ingestions excludes decided runs", rid2 not in [r["id"] for r in gates.open_ingestions()])
check("open_ingestions includes waiting runs", rid3 in [r["id"] for r in gates.open_ingestions()])

# --- the rest of the bot respects ingestion ---------------------------------
check("sheet status shows ingestion while open", gates.status_line(state(rid3)).startswith("Ingestion"))
check("ship is blocked until ingestion is signed off",
      "Opportunity ingestion not signed off" in gates.ship_blockers(state(rid3)))
check("a finished ingestion no longer blocks ship",
      "Opportunity ingestion not signed off" not in gates.ship_blockers(state(rid)))
check("requestor is an owner role", gates.SIGNOFF_LABELS.get("requestor") == "Requestor")

# --- the message ------------------------------------------------------------
def actions(blocks):
    return [e.get("action_id", "") for b in blocks if b["type"] == "actions" for e in b["elements"]]

open_blocks = ingestion_blocks.blocks(gates.get_run(rid3))
json.dumps(open_blocks)
ids = actions(open_blocks)
check("open ingestion offers element sign-off", any(i.startswith("ingest_check:") for i in ids))
check("open ingestion offers go, no-go and a question",
      all(any(i.startswith(p + ":") for i in ids) for p in ("ingest_go", "ingest_nogo", "ingest_ask")))
check("no final sign-off before go", not any(i.startswith("ingest_sign:") for i in ids))
check("values are shown", "500–800" in json.dumps(open_blocks, ensure_ascii=False))
check("under 50 blocks", len(open_blocks) <= 50, str(len(open_blocks)))

go_run = new_run()
ingestion.set_signed(go_run, ALL, DEL)
ingestion.decide(go_run, "go", DEL)
ids = actions(ingestion_blocks.blocks(gates.get_run(go_run)))
check("after go, the sign-off buttons appear", sum(i.startswith("ingest_sign:") for i in ids) == 2)
check("after go, no more element ticks", not any(i.startswith("ingest_check:") for i in ids))

done_blocks = json.dumps(ingestion_blocks.blocks(gates.get_run(rid)))
check("a finished ingestion says so", "Ingestion signed off" in done_blocks)

gates.close_run(rid3, DEL, "stalled")
check("a closed ingestion has no controls", actions(ingestion_blocks.blocks(gates.get_run(rid3))) == [])

huge = dict(RECORD, spec_doc="x" * 5000, crucial_details="y" * 5000)
big = ingestion_blocks.blocks(gates.get_run(new_run(huge)))
longest = max(len(b["text"]["text"]) for b in big if b["type"] == "section")
check("long answers stay under Slack's limit", longest <= 3000, str(longest))

print()
print(f"{len(failures)} failure(s)" if failures else "all checks passed")
raise SystemExit(1 if failures else 0)
