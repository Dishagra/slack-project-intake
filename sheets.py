"""Append intake submissions to a Google Sheet, and keep their gate status current.

Columns are managed automatically: any key not already in the header row is
appended as a new column, so editing form_schema.json never requires touching
the sheet by hand. If credentials are absent the module degrades to a no-op so
the bot still works channel-post-only.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
WORKSHEET = os.environ.get("GOOGLE_WORKSHEET_NAME", "Intake")
CREDS_PATH = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

# Written on every row so gate updates can find their row again later.
RUN_ID_COL = "run_id"
STATUS_COL = "gate_status"

# The columns worth seeing first, in the order someone scanning the sheet wants
# them. Anything not listed keeps its schema order and follows behind, so new
# fields still land automatically without being promoted to the front.
DOC_COL = "doc_url"

LEADING_COLUMNS = [
    "project_name",
    "account",
    "project_type",
    STATUS_COL,
    DOC_COL,
    "urgency",
    "target_date",
    "research_owner",
    "delivery_owner",
    "sales_owner",
    "workstream",
    "submitted_at",
    "submitted_by",
]

# Bookkeeping the team never needs to read; parked at the far right.
TRAILING_COLUMNS = [RUN_ID_COL]

# Header text is the field's own label where we can find one, so the sheet reads
# like the form rather than like the code.
_HEADER_OVERRIDES = {
    "account": "Customer",
    "project_type": "Type",
    STATUS_COL: "Status",
    "submitted_at": "Submitted",
    "submitted_by": "Submitted by",
    RUN_ID_COL: "Run ID",
    DOC_COL: "Project doc",
}


def _labels() -> Dict[str, str]:
    """Column key -> human header, drawn from the form schema."""
    try:
        from form_builder import load_schema

        schema = load_schema()
        fields = list(schema.get("common_fields", []))
        for defn in schema.get("project_types", {}).values():
            fields.extend(defn.get("fields", []))
        labels = {f["key"]: f["label"] for f in fields if f.get("type") != "context"}
    except Exception:  # the sheet must still work if the schema will not load
        labels = {}
    labels.update(_HEADER_OVERRIDES)
    return labels


def order_columns(keys: List[str]) -> List[str]:
    """Leading columns first, then the rest in the order they arrived."""
    rest = [k for k in keys if k not in LEADING_COLUMNS and k not in TRAILING_COLUMNS]
    return (
        [k for k in LEADING_COLUMNS if k in keys]
        + rest
        + [k for k in TRAILING_COLUMNS if k in keys]
    )

# gspread's client is not documented as thread-safe; Bolt dispatches listeners
# on a pool, so serialise every read/modify/write pair.
_lock = threading.Lock()
_worksheet = None


def enabled() -> bool:
    # The key file has to exist, not just be configured — a missing key would
    # otherwise turn every write into a failure that DMs whoever submitted.
    return bool(SHEET_ID and CREDS_PATH and os.path.exists(CREDS_PATH))


def sheet_url() -> Optional[str]:
    return f"https://docs.google.com/spreadsheets/d/{SHEET_ID}" if SHEET_ID else None


def _get_worksheet():
    global _worksheet
    if _worksheet is not None:
        return _worksheet

    import gspread  # imported lazily so the bot runs without the dependency

    client = gspread.service_account(filename=CREDS_PATH)
    spreadsheet = client.open_by_key(SHEET_ID)
    try:
        _worksheet = spreadsheet.worksheet(WORKSHEET)
    except gspread.WorksheetNotFound:
        _worksheet = spreadsheet.add_worksheet(title=WORKSHEET, rows=1000, cols=40)

    # gspread defaults to no timeout at all. The sheet writer is a single thread,
    # so one hung socket would park it permanently and every later write would
    # queue behind it — the sheet just quietly stops updating.
    try:
        client.set_timeout((5, 30))
    except Exception:
        logger.warning("Could not set a request timeout on the Sheets client", exc_info=True)

    return _worksheet


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


class HeaderMismatch(Exception):
    """A header cell matches neither a known label nor a known key."""


def _read_keys(ws, strict: bool = True) -> List[str]:
    """The header row, translated from displayed labels back to field keys.

    Row 1 holds human labels so the sheet reads like the form. Labels are unique
    across the schema (test_form.py enforces it), so the mapping is reversible.

    An unrecognised cell is refused rather than assumed to be a key. Assuming
    forks the column silently: rename a label in form_schema.json and the old
    heading stops matching, so a second column appears and the history is
    stranded in a dead one. Worse for "Run ID" and "Status", where a single
    edited heading stops every run from being found again.
    """
    labels = _labels()
    reverse = {v: k for k, v in labels.items()}
    header = ws.row_values(1)

    unknown = [c for c in header if c and c not in reverse and c not in labels]
    if unknown and strict:
        raise HeaderMismatch(
            "These sheet headings match no field: "
            + ", ".join(repr(c) for c in unknown)
            + ". Either a label was renamed in form_schema.json or the sheet was "
            "edited by hand. Restore the heading, or run check_sheets.py --tidy "
            "after putting form_schema.json back."
        )

    return [reverse.get(cell, cell) for cell in header]


def _write_header(ws, keys: List[str]) -> None:
    labels = _labels()
    ws.update(range_name="A1", values=[[labels.get(k, k) for k in keys]])


def _style_header(ws, column_count: int) -> None:
    """Freeze and bold row 1, and put a filter on it. Cosmetic, so never fatal."""
    try:
        ws.freeze(rows=1)
        ws.format(
            "1:1",
            {
                "textFormat": {"bold": True},
                "backgroundColor": {"red": 0.94, "green": 0.94, "blue": 0.96},
                "verticalAlignment": "MIDDLE",
                "wrapStrategy": "CLIP",
            },
        )
        ws.set_basic_filter()
        ws.columns_auto_resize(0, min(column_count, 26))
    except Exception:
        logger.warning("Could not style the sheet header", exc_info=True)


def _ensure_columns(ws, keys: List[str], wanted: List[str]) -> List[str]:
    """Extend the header with any new columns.

    New columns are appended at the far right and an existing header is never
    reordered. Reordering only row 1 would leave every row already written sitting
    under the wrong headings — the readable order is applied when the header is
    first created, and afterwards only by tidy(), which moves the data with it.
    """
    missing = [c for c in wanted if c not in keys]
    if not missing:
        return keys

    if keys:
        updated = keys + missing
    else:
        # An empty header over existing rows is not a new sheet — it is usually
        # someone who cleared row 1's text instead of deleting the row. Writing
        # a freshly-ordered header there would file every existing value under
        # the wrong heading, and tidy() would then make that permanent.
        if len(ws.get_all_values()) > 1:
            raise HeaderMismatch(
                "The sheet has data but no header row. Restore row 1 before the "
                "bot writes again — writing a new header now would mis-file every "
                "existing row."
            )
        updated = order_columns(missing)

    _write_header(ws, updated)
    _style_header(ws, len(updated))
    return updated


def tidy() -> Optional[int]:
    """Rewrite the whole sheet into the readable column order.

    Header and every data row are rewritten together, so columns added over time
    end up where a reader expects them. Values are re-filed by key, never by
    position. Returns the number of data rows moved.
    """
    if not enabled():
        return None

    with _lock:
        ws = _get_worksheet()
        # Read unformatted: get_all_values returns what the cells *display*, so a
        # formatted number would be rewritten as the literal text "50,000" and a
        # date as "8/14/2026". Round-tripping through display text is one-way.
        import gspread.utils

        values = ws.get_all_values(value_render_option=gspread.utils.ValueRenderOption.unformatted)
        if len(values) < 2:
            return 0

        keys = _read_keys(ws)
        records = [dict(zip(keys, row)) for row in values[1:]]
        ordered = order_columns(keys)

        _write_header(ws, ordered)
        ws.update(
            range_name="A2",
            values=[[record.get(k, "") for k in ordered] for record in records],
        )
        _style_header(ws, len(ordered))

    return len(records)


def append_row(record: Dict[str, Any]) -> Optional[str]:
    """Write one submission. Returns the sheet URL, or None if disabled."""
    if not enabled():
        logger.info("Sheets disabled (GOOGLE_SHEET_ID / GOOGLE_SERVICE_ACCOUNT_JSON unset)")
        return None

    with _lock:
        ws = _get_worksheet()
        keys = _ensure_columns(ws, _read_keys(ws), list(record.keys()))
        ws.append_row(
            [_stringify(record.get(col)) for col in keys],
            value_input_option="RAW",
        )

    return sheet_url()


def _locate(ws, run_id: str) -> Tuple[List[str], Optional[int]]:
    """Header keys plus the 1-indexed sheet row holding this run, if present."""
    keys = _read_keys(ws)
    if RUN_ID_COL not in keys:
        return keys, None
    values = ws.col_values(keys.index(RUN_ID_COL) + 1)
    for i, value in enumerate(values, start=1):
        if value == run_id:
            return keys, i
    return keys, None


def update_row(run_id: str, record: Dict[str, Any]) -> bool:
    """Rewrite one run's whole row after an edit. False if the row is not there.

    Columns absent from the new record are cleared rather than left behind, so
    an answer that was removed does not linger in the sheet looking current.
    Bookkeeping columns the edit does not carry — the run id, the gate status —
    are kept as they are.
    """
    if not enabled() or not run_id:
        return False

    with _lock:
        ws = _get_worksheet()
        keys, row = _locate(ws, run_id)
        if row is None:
            logger.warning("No sheet row for run %s; edit not written", run_id)
            return False

        keys = _ensure_columns(ws, keys, list(record.keys()))
        existing = ws.row_values(row)
        preserved = {RUN_ID_COL, STATUS_COL, DOC_COL}

        values = []
        for i, key in enumerate(keys):
            if key in preserved:
                values.append(existing[i] if i < len(existing) else "")
            else:
                values.append(_stringify(record.get(key)))

        ws.update(range_name=f"A{row}", values=[values])

    return True


def update_status(run_id: str, status: str) -> bool:
    """Rewrite one run's gate status cell. Returns False if the row is gone."""
    if not enabled() or not run_id:
        return False

    with _lock:
        ws = _get_worksheet()
        keys, row = _locate(ws, run_id)
        if row is None:
            logger.warning("No sheet row for run %s; gate status not written", run_id)
            return False
        keys = _ensure_columns(ws, keys, [STATUS_COL])
        ws.update_cell(row, keys.index(STATUS_COL) + 1, status)

    return True
