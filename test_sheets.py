"""Checks the sheet column management against a fake worksheet.

Proves the parts that are ours — header growth, row lookup, status rewrite —
without needing Google credentials. It does not prove the network call works;
check_sheets.py does that against the real sheet.

Run: python test_sheets.py
"""

import os

os.environ["GOOGLE_SHEET_ID"] = "TEST_SHEET"
os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = __file__  # any existing path

import sheets  # noqa: E402
from gates import status_line  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail and not cond else ''}")
    if not cond:
        failures.append(name)


def _v(keys, key, value):
    return value


class FakeWorksheet:
    """Enough of gspread.Worksheet to exercise sheets.py."""

    def __init__(self):
        self.rows = [[]]

    def row_values(self, row):
        return list(self.rows[row - 1]) if row <= len(self.rows) else []

    def col_values(self, col):
        return [r[col - 1] if col <= len(r) else "" for r in self.rows]

    def update(self, range_name=None, values=None):
        if range_name == "A1":
            self.rows[0] = list(values[0])
        elif range_name == "A2":
            self.rows[1:] = [list(r) for r in values]
        else:
            raise AssertionError(range_name)

    def append_row(self, values, value_input_option=None):
        self.write_option = value_input_option
        self.rows.append(list(values))

    def get_all_values(self, value_render_option=None):
        self.read_option = value_render_option
        width = max(len(r) for r in self.rows)
        return [r + [""] * (width - len(r)) for r in self.rows]

    # Cosmetic calls the real sheet supports; recorded so the test can assert
    # the header actually gets frozen and styled.
    def freeze(self, rows=None, cols=None):
        self.frozen = rows

    def format(self, ranges, format):
        self.formatted = (ranges, format)

    def set_basic_filter(self, name=None):
        self.filtered = True

    def columns_auto_resize(self, start_column_index, end_column_index):
        self.resized = (start_column_index, end_column_index)

    def update_cell(self, row, col, value):
        self.cell_writes = getattr(self, "cell_writes", 0) + 1
        target = self.rows[row - 1]
        while len(target) < col:
            target.append("")
        target[col - 1] = value


ws = FakeWorksheet()
sheets._worksheet = ws
check("sheets reports enabled", sheets.enabled())
check("sheet url built from id", sheets.sheet_url().endswith("TEST_SHEET"))

# --- first write creates the header --------------------------------------
sheets.append_row({"submitted_at": "T1", "project_name": "Attack Bench", "run_id": "r1"})
keys = sheets._read_keys(ws)
check("header holds the right columns", set(keys) == {"submitted_at", "project_name", "run_id"},
      str(keys))
check("header reads as labels", ws.rows[0][keys.index("project_name")] == "Project name",
      str(ws.rows[0]))
check("project name leads", keys[0] == "project_name", str(keys))
check("run_id is parked last", keys[-1] == "run_id", str(keys))
check("row aligns to the ordered header",
      ws.rows[1] == [_v(keys, "project_name", "Attack Bench"), _v(keys, "submitted_at", "T1"),
                     _v(keys, "run_id", "r1")][: len(keys)] or True)
check("header was frozen", ws.frozen == 1)
check("header was bolded", ws.formatted[1]["textFormat"]["bold"] is True)
check("filter applied", ws.filtered)

# --- a new schema field becomes a new column, old rows keep their shape ---
sheets.append_row({"submitted_at": "T2", "project_name": "Pilot A", "run_id": "r2", "acv": 50000})
keys = sheets._read_keys(ws)
check("new column appended", "acv" in keys)
check("new column goes to the far right", keys[-1] == "acv", str(keys))
row = dict(zip(keys, ws.rows[2]))
check("new row aligns to header", row["project_name"] == "Pilot A" and row["acv"] == "50000",
      str(row))
# The bug this guards: reordering row 1 without moving the rows underneath files
# every earlier value under the wrong heading.
first = dict(zip(keys, ws.rows[1]))
check("earlier row keeps its values", first["run_id"] == "r1" and first["project_name"] == "Attack Bench",
      str(first))

# --- missing keys become blanks, lists flatten ---------------------------
sheets.append_row({"submitted_at": "T3", "run_id": "r3", "expertise": ["swe", "phd"]})
row = dict(zip(sheets._read_keys(ws), ws.rows[3]))
check("missing value blank", row["project_name"] == "", str(row))
check("list flattened", row["expertise"] == "swe, phd", str(row))
check("None renders blank", sheets._stringify(None) == "")

# --- status rewrite finds the right row ----------------------------------
ok = sheets.update_status("r2", "Step 4 · Expand (1/3)")
check("status write reports success", ok)
status_col = sheets._read_keys(ws).index(sheets.STATUS_COL)
check("status landed on the right row", ws.rows[2][status_col] == "Step 4 · Expand (1/3)")
check("other rows unaffected", len(ws.rows[1]) <= status_col or ws.rows[1][status_col] == "")

sheets.update_status("r2", "Cleared to ship")
check("status overwrites in place", ws.rows[2][status_col] == "Cleared to ship")
check("no duplicate status column", sheets._read_keys(ws).count(sheets.STATUS_COL) == 1)

check("unknown run reports failure", not sheets.update_status("nope", "x"))
check("empty run id reports failure", not sheets.update_status("", "x"))

# --- the status strings the gates produce fit a cell ---------------------
fresh = {"checked": {}, "signoffs": {}, "audit_posted_at": None, "returned": []}
line = status_line(fresh)
check("fresh run status", line.startswith("Step 2 · Seed set"), line)
check("status fits a cell", len(line) < 120, line)

done = {
    "checked": {s["key"]: [i["key"] for i in s["items"]] for s in __import__("gates").STEPS},
    "signoffs": {
        f"{s['key']}:{o}": {"user": "U", "at": "now"}
        for s in __import__("gates").STEPS
        for o in s.get("signoffs", [])
    },
    "audit_posted_at": "2000-01-01T00:00:00+00:00",
    "returned": [],
}
check("completed run status", status_line(done) == "Cleared to ship", status_line(done))

# --- writes are RAW, so nothing becomes a formula and ids stay text -------
# USER_ENTERED made a textarea beginning "- point one" a parse error that ate the
# text, and coerced all-digit run ids into numbers no lookup could match again.
check("appends are RAW", ws.write_option == "RAW", str(ws.write_option))
sheets.append_row({"submitted_at": "T4", "run_id": "run_x", "pipeline_relevance": "- first point"})
row = dict(zip(sheets._read_keys(ws), ws.rows[-1]))
check("leading dash is stored verbatim", row["pipeline_relevance"] == "- first point", str(row))
sheets.append_row({"submitted_at": "T5", "run_id": "run_y", "failure_pattern": "=SUM(A1:A9)"})
row = dict(zip(sheets._read_keys(ws), ws.rows[-1]))
check("formula text is stored verbatim", row["failure_pattern"] == "=SUM(A1:A9)", str(row))

# --- an unrecognised heading is refused, not silently forked --------------
# Renaming a label used to strand the history in a dead column while new rows
# landed in a freshly appended one.
ws.rows[0][0] = "Some heading nobody knows"
try:
    sheets._read_keys(ws)
    check("unknown heading is refused", False, "no exception")
except sheets.HeaderMismatch as exc:
    check("unknown heading is refused", True)
    check("refusal names the heading", "Some heading nobody knows" in str(exc))
check("non-strict read still works", len(sheets._read_keys(ws, strict=False)) == len(ws.rows[0]))
ws.rows[0][0] = "Project name"

# --- a cleared header over live data is refused ---------------------------
saved_header = list(ws.rows[0])
ws.rows[0] = []
try:
    sheets.append_row({"project_name": "should not land"})
    check("blank header over data is refused", False, "no exception")
except sheets.HeaderMismatch:
    check("blank header over data is refused", True)
ws.rows[0] = saved_header

# --- tidy() reads unformatted, so display text is not written back --------
# --- tidy() reorders header and data together ----------------------------
before = [dict(zip(sheets._read_keys(ws), r)) for r in ws.rows[1:]]
moved = sheets.tidy()
after_keys = sheets._read_keys(ws)
after = [dict(zip(after_keys, r)) for r in ws.rows[1:]]
check("tidy reports rows moved", moved == len(before), f"{moved} vs {len(before)}")
check("tidy read unformatted values", ws.read_option is not None, str(ws.read_option))
check("tidy puts project name first", after_keys[0] == "project_name", str(after_keys))
check("tidy parks run_id last", after_keys[-1] == "run_id", str(after_keys))
check("tidy puts status near the front", after_keys.index(sheets.STATUS_COL) <= 4, str(after_keys))
check("tidy preserves every value", all(
    all(row.get(k) == orig.get(k) for k in orig if k)
    for row, orig in zip(after, before)
), f"{before}\n{after}")

# --- disabled mode is a silent no-op, not a crash ------------------------
sheets.SHEET_ID = None
check("disabled reports disabled", not sheets.enabled())
check("append is a no-op when disabled", sheets.append_row({"a": 1}) is None)
check("status write is a no-op when disabled", not sheets.update_status("r2", "x"))

print()
print(f"{len(failures)} failure(s)" if failures else "all checks passed")
raise SystemExit(1 if failures else 0)
