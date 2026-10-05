"""Preflight for the Google Sheets connection.

Read-only by default: authenticates, opens the spreadsheet, and reports what it
found. Pass --write to also append one clearly-labelled test row, which you then
delete yourself — this script never deletes anything.

Run: python check_sheets.py [--write]
"""

from __future__ import annotations

import json
import os
import sys

import sheets


def fail(message: str, fix: str) -> None:
    print(f"\n  FAIL  {message}\n        {fix}\n")
    sys.exit(1)


def main() -> None:
    write = "--write" in sys.argv

    print("Google Sheets preflight\n" + "-" * 40)

    print(f"GOOGLE_SHEET_ID              {sheets.SHEET_ID or '(unset)'}")
    print(f"GOOGLE_WORKSHEET_NAME        {sheets.WORKSHEET}")
    print(f"GOOGLE_SERVICE_ACCOUNT_JSON  {sheets.CREDS_PATH or '(unset)'}")

    if not sheets.enabled():
        fail(
            "Sheets is not configured.",
            "Set GOOGLE_SHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON in .env, then "
            "re-run with: set -a && source .env && set +a && .venv/bin/python check_sheets.py",
        )

    if not os.path.exists(sheets.CREDS_PATH):
        fail(
            f"No key file at {sheets.CREDS_PATH}.",
            "Point GOOGLE_SERVICE_ACCOUNT_JSON at the JSON key you downloaded "
            "from the Google Cloud console.",
        )

    try:
        with open(sheets.CREDS_PATH, encoding="utf-8") as f:
            creds = json.load(f)
    except json.JSONDecodeError:
        fail("The key file is not valid JSON.", "Re-download it; it may have been truncated.")

    email = creds.get("client_email")
    if not email:
        fail(
            "The key file has no client_email.",
            "That is an OAuth client secret, not a service-account key. Create a "
            "service account, then add a key to it.",
        )
    print(f"\nService account               {email}")

    try:
        import gspread
    except ImportError:
        fail("gspread is not installed.", "Run: .venv/bin/pip install -r requirements.txt")

    try:
        client = gspread.service_account(filename=sheets.CREDS_PATH)
    except Exception as exc:
        fail(f"Could not authenticate: {exc}", "Check the key file is the current, unrevoked one.")

    try:
        spreadsheet = client.open_by_key(sheets.SHEET_ID)
    except gspread.SpreadsheetNotFound:
        # 404: the id itself is wrong. Sharing cannot fix a sheet that is not there.
        fail(
            "No spreadsheet has that id.",
            "GOOGLE_SHEET_ID is the long string between /d/ and /edit in the sheet "
            "URL — not the whole URL, and not the #gid at the end.",
        )
    except PermissionError as exc:
        # gspread raises the *builtin* PermissionError for 403, not one of its own
        # exception types — and 403 covers both of the common setup mistakes.
        detail = str(exc.__cause__ or exc) or "(Google returned no detail)"
        fail(
            "Google refused access to that spreadsheet.",
            f"Either the sheet is not shared, or the API is off.\n"
            f"        1. Open the sheet, press Share, give {email} Editor access.\n"
            f"        2. Check the Sheets API and Drive API are enabled for this project.\n"
            f"        Google said: {detail}",
        )
    except gspread.exceptions.APIError as exc:
        detail = str(exc)
        if "disabled" in detail or "has not been used" in detail:
            fail(
                "The Google Sheets API is not enabled for this project.",
                "Enable it in the Cloud console under APIs & Services, then wait "
                "a minute and re-run.",
            )
        fail(f"Google refused the request: {detail}", "The message above names the cause.")

    print(f"Spreadsheet                   {spreadsheet.title}")
    tabs = [w.title for w in spreadsheet.worksheets()]
    print(f"Tabs                          {', '.join(tabs)}")

    if sheets.WORKSHEET in tabs:
        ws = spreadsheet.worksheet(sheets.WORKSHEET)
        header = ws.row_values(1)
        rows = len(ws.col_values(1))
        print(f"Target tab                    {sheets.WORKSHEET} — {len(header)} columns, "
              f"{max(rows - 1, 0)} data row(s)")
        if header:
            print(f"Header                        {', '.join(header[:8])}"
                  f"{' …' if len(header) > 8 else ''}")
    else:
        print(f"Target tab                    {sheets.WORKSHEET} — will be created on first write")

    if "--tidy" in sys.argv:
        print("\nReordering columns and rewriting every row…")
        try:
            moved = sheets.tidy()
        except sheets.HeaderMismatch as exc:
            fail("The header row does not match the form.", str(exc))
        except Exception as exc:
            fail(f"Could not rewrite the sheet: {exc}", "Nothing was changed.")
        print(f"\n  OK  {moved} row(s) rewritten in readable column order.")
        print(f"      {sheets.sheet_url()}")
        return

    if not write:
        print("\n  OK  Read access confirmed.")
        print("      Write access is not proven by a read. To prove it, run:")
        print("        .venv/bin/python check_sheets.py --write")
        print("      To reorder the columns of an existing sheet, run:")
        print("        .venv/bin/python check_sheets.py --tidy")
        return

    print("\nAppending one test row…")
    try:
        sheets.append_row(
            {
                "submitted_at": "PREFLIGHT TEST ROW",
                "submitted_by": "check_sheets.py",
                "project_name": "DELETE ME — connection test",
                sheets.RUN_ID_COL: "preflight",
                sheets.STATUS_COL: "test",
            }
        )
    except Exception as exc:
        fail(
            f"Write failed: {exc}",
            f"The service account can read but not write. Re-share the sheet with "
            f"{email} as Editor, not Viewer.",
        )

    print("\n  OK  Read and write both confirmed.")
    print(f"      Delete the row labelled 'DELETE ME — connection test' from the "
          f"{sheets.WORKSHEET} tab yourself.")
    print(f"      {sheets.sheet_url()}")


if __name__ == "__main__":
    main()
