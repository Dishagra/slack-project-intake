"""Editing an intake after it was submitted.

Covers the record update and its diff, the sheet row rewrite, and the trap that
a re-render inside the edit modal must not turn a save into a new submission.

Run: python test_edit.py
"""

import os
import tempfile

os.environ["GATES_DB"] = os.path.join(tempfile.mkdtemp(), "edit_test.db")
os.environ["GOOGLE_SHEET_ID"] = "TEST_SHEET"
os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = __file__
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test")

import functools  # noqa: E402

import slack_bolt  # noqa: E402

slack_bolt.App.__init__ = functools.partialmethod(
    slack_bolt.App.__init__, token_verification_enabled=False, request_verification_enabled=False
)

import app  # noqa: E402
import gates  # noqa: E402
import sheets  # noqa: E402
from admin_views import EDIT_CALLBACK, summary_blocks  # noqa: E402
from form_builder import CALLBACK_ID, TYPE_KEY, build_view, load_schema  # noqa: E402

SCHEMA = load_schema()
failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail and not cond else ''}")
    if not cond:
        failures.append(name)


ORIGINAL = {
    TYPE_KEY: "sample",
    "project_name": "Attack Bench",
    "account": "Tencnt",              # deliberate typo, to be corrected
    "workstream": "red_team",
    "research_owner": "U_RES",
    "delivery_owner": "U_DEL",
    "sales_owner": "U_SAL",
    "urgency": "high",
    "target_date": "2026-09-30",
    "existing_asset": "no",
    "go_decision": "go",
    "intake_stage": "scoping",
}

gates.init_db()
run_id = gates.create_run(
    "C1", "170.1", "Attack Bench", "sample",
    {"research_owner": "U_RES", "delivery_owner": "U_DEL"}, record=ORIGINAL,
)
gates.set_message_ts(run_id, "170.2")

# --- the edit modal is the intake form, pre-filled -----------------------
run = gates.get_run(run_id)
view = build_view(
    SCHEMA, run["project_type"], {**run["record"], TYPE_KEY: run["project_type"]},
    "", callback_id=EDIT_CALLBACK, submit_label="Save changes",
)
by_id = {b.get("block_id"): b for b in view["blocks"] if b.get("type") == "input"}
check("edit form is pre-filled", by_id["account"]["element"]["initial_value"] == "Tencnt")
check("edit form keeps the type", by_id[TYPE_KEY]["element"]["initial_option"]["value"] == "sample")
check("edit form routes to the edit handler", view["callback_id"] == EDIT_CALLBACK)
check("edit form says save, not submit", view["submit"]["text"] == "Save changes")
check("edit form is titled as an edit", view["title"]["text"] == "Edit intake")

# --- a re-render inside the edit modal must stay an edit -----------------
# Without this, touching any dropdown mid-edit rebuilt the view with the default
# callback, and saving would have filed a second intake instead of updating one.
sent = []


class FakeClient:
    def views_update(self, **kwargs):
        sent.append(kwargs)


body = {"view": {"id": "V", "hash": "h", "private_metadata": "",
                 "callback_id": EDIT_CALLBACK, "blocks": view["blocks"]}}
app._rerender(FakeClient(), body, "sample", {**run["record"], "urgency": "critical"})
check("re-render happened", len(sent) == 1)
check("re-render keeps the edit callback", sent[0]["view"]["callback_id"] == EDIT_CALLBACK,
      sent[0]["view"]["callback_id"])
check("re-render keeps the save label", sent[0]["view"]["submit"]["text"] == "Save changes")

sent.clear()
new_body = {"view": {"id": "V", "hash": "h", "private_metadata": "",
                     "callback_id": CALLBACK_ID, "blocks": view["blocks"]}}
app._rerender(FakeClient(), new_body, "sample", {**run["record"], "urgency": "critical"})
check("a new intake still routes to the intake handler",
      sent[0]["view"]["callback_id"] == CALLBACK_ID)

# --- the diff describes what actually changed ---------------------------
edited = dict(ORIGINAL)
edited["account"] = "Tencent"
edited["urgency"] = "critical"
edited["urgency_justification"] = "Eval window closes this quarter."
edited["target_date"] = "2026-10-15"

changes = gates.update_record(run_id, edited, "Attack Bench", "sample", "U_RES")
check("every change is reported", len(changes) == 4, str(changes))
joined = " | ".join(changes)
check("diff shows the corrected customer", "Tencnt → Tencent" in joined, joined)
check("diff uses labels, not keys", "Customer" in joined and "account" not in joined, joined)
check("diff shows a newly filled field", "_empty_ →" in joined, joined)
check("record is updated", gates.get_run(run_id)["record"]["account"] == "Tencent")
check("edit is recorded in history", len(gates.get_run(run_id)["state"]["edits"]) == 1)
check("editor is recorded", gates.get_run(run_id)["state"]["edits"][0]["by"] == "U_RES")

# --- an edit that changes nothing is not recorded -----------------------
again = gates.update_record(run_id, edited, "Attack Bench", "sample", "U_RES")
check("no-op edit reports nothing", again == [])
check("no-op edit adds no history", len(gates.get_run(run_id)["state"]["edits"]) == 1)

# --- the checklist is untouched by an edit ------------------------------
gates.set_checked(run_id, "seed", ["fidelity", "surfaces"])
gates.update_record(run_id, {**edited, "project_name": "Attack Bench v2"},
                    "Attack Bench v2", "sample", "U_DEL")
state = gates.get_run(run_id)["state"]
check("ticks survive an edit", state["checked"]["seed"] == ["fidelity", "surfaces"])
check("renaming updates the run", gates.get_run(run_id)["project_name"] == "Attack Bench v2")

# --- editing a missing run raises rather than crashing ------------------
try:
    gates.update_record("run_nope", edited, "x", "sample", "U_RES")
    check("editing a missing run raises RunGone", False, "no exception")
except gates.RunGone:
    check("editing a missing run raises RunGone", True)

# --- the sheet row is rewritten, not appended ---------------------------
class FakeWorksheet:
    def __init__(self):
        self.rows = [[]]

    def row_values(self, row):
        return list(self.rows[row - 1]) if row <= len(self.rows) else []

    def col_values(self, col):
        return [r[col - 1] if col <= len(r) else "" for r in self.rows]

    def update(self, range_name=None, values=None):
        if range_name == "A1":
            self.rows[0] = list(values[0])
        else:
            index = int(range_name[1:]) - 1
            while len(self.rows) <= index:
                self.rows.append([])
            self.rows[index] = list(values[0])

    def append_row(self, values, value_input_option=None):
        self.rows.append(list(values))

    def get_all_values(self, value_render_option=None):
        width = max(len(r) for r in self.rows)
        return [r + [""] * (width - len(r)) for r in self.rows]

    def update_cell(self, row, col, value):
        target = self.rows[row - 1]
        while len(target) < col:
            target.append("")
        target[col - 1] = value

    def freeze(self, rows=None, cols=None):
        pass

    def format(self, ranges, format):
        pass

    def set_basic_filter(self, name=None):
        pass

    def columns_auto_resize(self, a, b):
        pass


ws = FakeWorksheet()
sheets._worksheet = ws
sheets.append_row({**ORIGINAL, "run_id": run_id, "gate_status": "Step 2 · Seed set (0/3)"})
sheets.append_row({**ORIGINAL, "run_id": "run_other", "project_name": "Untouched"})
rows_before = len(ws.rows)

check("sheet row rewritten", sheets.update_row(run_id, edited))
check("no row was appended", len(ws.rows) == rows_before, f"{len(ws.rows)} vs {rows_before}")

keys = sheets._read_keys(ws)
row = dict(zip(keys, ws.rows[1]))
check("edited value landed", row["account"] == "Tencent", str(row.get("account")))
check("run_id is preserved", row["run_id"] == run_id)
check("gate status is preserved", row["gate_status"] == "Step 2 · Seed set (0/3)",
      str(row.get("gate_status")))
other = dict(zip(keys, ws.rows[2]))
check("the other run is untouched", other["project_name"] == "Untouched")

# a removed answer is cleared, not left looking current
trimmed = {k: v for k, v in edited.items() if k != "urgency_justification"}
sheets.update_row(run_id, trimmed)
row = dict(zip(sheets._read_keys(ws), ws.rows[1]))
check("removed answer is cleared", row.get("urgency_justification") == "",
      repr(row.get("urgency_justification")))

check("editing an unknown run reports failure", not sheets.update_row("run_nope", edited))

# --- the summary carries an Edit button ---------------------------------
blocks = summary_blocks(run_id, "Attack Bench", "Sample", "U_RES", edited,
                        app.OWNER_KEYS, None)
actions = [e for b in blocks if b["type"] == "actions" for e in b["elements"]]
check("summary offers Edit", any(a["action_id"].startswith("intake_edit:") for a in actions))
check("summary still offers the detail view",
      any(a["action_id"].startswith("intake_detail:") for a in actions))

# --- long answers must not break the views ------------------------------
# Slack rejects a whole view if any section text passes 3000 characters, and it
# fails after ack(), so the button just stops working for that project. This was
# verified by hand once and then silently lost in a rebuild; it is pinned here.
from admin_views import SECTION_LIMIT, detail_view  # noqa: E402
from gate_blocks import checklist_blocks  # noqa: E402

huge = {TYPE_KEY: "sample", "intake_stage": "ready", "project_name": "P" * 3000}
for field in SCHEMA["common_fields"] + SCHEMA["project_types"]["sample"]["fields"]:
    if field["type"] in ("text", "textarea"):
        huge[field["key"]] = "x" * 3000
big_run = {"id": "run_big", "project_name": "N" * 3000, "project_type": "sample",
           "record": huge, "owners": {}, "state": {"checked": {}, "signoffs": {}}}


def section_lengths(blocks):
    for b in blocks:
        if b.get("type") == "section":
            if "text" in b:
                yield len(b["text"]["text"])
            for f in b.get("fields", []):
                yield len(f["text"])


detail = list(section_lengths(detail_view(big_run)["blocks"]))
check("detail view stays under Slack's section limit", max(detail) <= SECTION_LIMIT, str(max(detail)))
summary = list(section_lengths(summary_blocks("run_big", "N" * 3000, "Sample", "U1", huge,
                                              app.OWNER_KEYS, None)))
check("summary stays under the limit with a huge name", max(summary) <= SECTION_LIMIT, str(max(summary)))
checklist = list(section_lengths(checklist_blocks(big_run)))
check("checklist title stays under the limit", max(checklist) <= SECTION_LIMIT, str(max(checklist)))

print()
print(f"{len(failures)} failure(s)" if failures else "all checks passed")
raise SystemExit(1 if failures else 0)
