"""The handlers, end to end: submit -> ingestion -> go -> sign-offs -> checklist.

Uses the real app module and a fake Slack client that records every call.

Run: python test_flow.py
"""

import functools
import os
import tempfile

os.environ["GATES_DB"] = os.path.join(tempfile.mkdtemp(), "flow.db")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test")
os.environ["GOOGLE_SHEET_ID"] = ""

import slack_bolt  # noqa: E402

slack_bolt.App.__init__ = functools.partialmethod(
    slack_bolt.App.__init__, token_verification_enabled=False, request_verification_enabled=False
)

import app  # noqa: E402
import gates  # noqa: E402
import ingestion  # noqa: E402
from form_builder import TYPE_KEY, is_input, visible_fields  # noqa: E402

gates.init_db()
failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail and not cond else ''}")
    if not cond:
        failures.append(name)


class FakeClient:
    def __init__(self):
        self.posts, self.updates, self.ephemerals = [], [], []

    def chat_postMessage(self, **kw):
        self.posts.append(kw)
        return {"ts": f"{len(self.posts)}.000", "channel": kw["channel"] if kw["channel"].startswith("C") else "D1"}

    def chat_update(self, **kw):
        self.updates.append(kw)

    def chat_postEphemeral(self, **kw):
        self.ephemerals.append(kw)

    def views_open(self, **kw):
        return {"view": {"id": "V9"}}


REQ, DEL, RES = "U_REQ", "U_DEL", "U_RES"


def submit(state):
    client = FakeClient()
    fields = [f for f in visible_fields(app.SCHEMA, state[TYPE_KEY], state) if is_input(f)]
    body = {"user": {"id": REQ, "username": "req"}}
    view = {"private_metadata": '{"channel_id": "C1"}'}
    app._record_submission(body, client, view, state, state[TYPE_KEY], fields)
    run_id = gates.get_run(next(iter(_all_ids())))["id"] if False else _latest()
    return client, run_id


def _all_ids():
    with gates._lock, gates._connect() as conn:
        return [r["id"] for r in conn.execute("SELECT id FROM runs ORDER BY created_at, rowid")]


def _latest():
    return _all_ids()[-1]


def texts(client):
    return [p.get("text", "") for p in client.posts]


BASE = {
    TYPE_KEY: "sample", "project_name": "MT Attack Bench", "request_signal": "customer_pilot",
    "requesting_team": "Research", "requestor": REQ, "account": "Tencent",
    "workstream": "red_team", "research_owner": RES, "delivery_owner": DEL,
    "sales_owner": "U_SAL", "urgency": "high", "target_date": "2026-11-30",
    "existing_asset": "no", "go_decision": "go", "intake_stage": "scoping",
}

# --- a new opportunity posts ingestion, not the checklist -----------------
client, rid = submit({**BASE, "ingestion_kind": "new", "spec_doc": "https://x.test/spec",
                      "task_volume": "500", "tat_agreed": "3 weeks", "milestones": "weekly"})
posted = texts(client)
check("summary posted", any("New Sample project" in t for t in posted), str(posted))
check("ingestion posted in the thread", any(t.startswith("Opportunity ingestion") for t in posted))
check("Delivery owner is pinged with the deadline", any(f"<@{DEL}>" in t and "36 hours" in t for t in posted))
check("the Steps 2–6 checklist waits", not any("Sample Creation Checklist" in t for t in posted))
run = gates.get_run(rid)
check("ingestion message is tracked", bool(run["state"]["ingestion"]["message_ts"]))
check("requestor is stored as an owner", run["owners"].get("requestor") == REQ)

# --- a recurring account skips straight to the checklist ------------------
client2, rid2 = submit({**BASE, "ingestion_kind": "recurring", "project_name": "GHealth wave 4"})
posted2 = texts(client2)
check("recurring skips ingestion", not any(t.startswith("Opportunity ingestion") for t in posted2))
check("recurring gets the checklist immediately", any("Sample Creation Checklist" in t for t in posted2))

# --- walk the handlers: tick, go, sign, sign ------------------------------
c = FakeClient()
ack = lambda **kw: None  # noqa: E731
body_del = {"user": {"id": DEL}, "channel": {"id": "C1"}}
body_req = {"user": {"id": REQ}, "channel": {"id": "C1"}}

app.on_ingest_check(ack, body_del, {"action_id": f"ingest_check:{rid}",
                                    "selected_options": [{"value": k} for k in ingestion.ELEMENT_KEYS]}, c)
check("ticking repaints the ingestion message", len(c.updates) == 1)

app.on_ingest_go(ack, body_del, {"action_id": f"ingest_go:{rid}"}, c)
check("go is announced", any("called *go*" in p["text"] for p in c.posts))

app.on_ingest_sign(ack, body_req, {"action_id": f"ingest_sign:{rid}:requestor"}, c)
check("no checklist after one signature", not any("Sample Creation Checklist" in p.get("text", "") for p in c.posts))

app.on_ingest_sign(ack, body_del, {"action_id": f"ingest_sign:{rid}:delivery_owner"}, c)
check("completion is announced", any("is signed off. Work starts" in p.get("text", "") for p in c.posts))
check("the checklist appears after both signatures",
      any("Sample Creation Checklist" in p.get("text", "") for p in c.posts))
check("the checklist is tracked", bool(gates.get_run(rid).get("message_ts")))

# signing again must not post a second checklist
before = len(c.posts)
app.on_ingest_sign(ack, body_del, {"action_id": f"ingest_sign:{rid}:delivery_owner"}, c)
check("no duplicate checklist on a repeat signature",
      sum("Sample Creation Checklist" in p.get("text", "") for p in c.posts[before:]) == 0)

# --- an outsider is refused, in the thread ---------------------------------
c3 = FakeClient()
_, rid3 = submit({**BASE, "ingestion_kind": "new", "project_name": "Other", "spec_doc": "https://x.test/s",
                  "task_volume": "1", "tat_agreed": "1w", "milestones": "m"})
app.on_ingest_go(ack, {"user": {"id": "U_RANDOM"}, "channel": {"id": "C1"}}, {"action_id": f"ingest_go:{rid3}"}, c3)
check("an outsider's go is refused privately", len(c3.ephemerals) == 1 and DEL in c3.ephemerals[0]["text"])
check("and nothing is announced", c3.posts == [])

# --- owners can change during ingestion ------------------------------------
_, rid4 = submit({**BASE, "ingestion_kind": "new", "project_name": "Handover", "spec_doc": "https://x.test/s",
                  "task_volume": "1", "tat_agreed": "1w", "milestones": "m"})
ids = [e.get("action_id", "") for b in __import__("ingestion_blocks").blocks(gates.get_run(rid4))
       if b["type"] == "actions" for e in b["elements"]]
check("Change owners is reachable from the ingestion message",
      any(i.startswith("gate_owners:") for i in ids))

# editing the requestor field moves the sign-off right with it
NEW_REQ = "U_NEWREQ"
ingestion.set_signed(rid4, ingestion.ELEMENT_KEYS, DEL)
ingestion.decide(rid4, "go", DEL)
run4 = gates.get_run(rid4)
record = dict(run4["record"], requestor=NEW_REQ)
fields = [f for f in visible_fields(app.SCHEMA, "sample", record) if is_input(f)]
app._apply_edit(FakeClient(), rid4, record, "sample", fields, DEL)
check("editing the requestor updates the owner list", gates.get_run(rid4)["owners"]["requestor"] == NEW_REQ)
ok, msg, _ = ingestion.sign_off(rid4, "requestor", REQ)
check("the previous requestor can no longer sign", not ok, msg)
ok, msg, _ = ingestion.sign_off(rid4, "requestor", NEW_REQ)
check("the new requestor can", ok, msg)

print()
print(f"{len(failures)} failure(s)" if failures else "all checks passed")
raise SystemExit(1 if failures else 0)
