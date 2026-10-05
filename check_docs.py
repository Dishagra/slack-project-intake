"""Preflight for the project-doc setup.

Checks the three things that have to be true, in the order they fail in practice:
the Docs API enabled, a Shared Drive the service account can reach, and write
access to it. Read-only unless --write is passed.

Run: python check_docs.py [--write]
"""

from __future__ import annotations

import json
import os
import sys

import project_docs


def fail(message: str, fix: str) -> None:
    print(f"\n  FAIL  {message}\n        {fix}\n")
    sys.exit(1)


def main() -> None:
    print("Project doc preflight\n" + "-" * 40)
    print(f"GOOGLE_DRIVE_FOLDER_ID       {project_docs.FOLDER_ID or '(unset)'}")
    print(f"GOOGLE_SERVICE_ACCOUNT_JSON  {project_docs.CREDS_PATH or '(unset)'}")

    if not project_docs.CREDS_PATH or not os.path.exists(project_docs.CREDS_PATH or ""):
        fail("No service-account key.", "Set GOOGLE_SERVICE_ACCOUNT_JSON in .env.")

    with open(project_docs.CREDS_PATH, encoding="utf-8") as f:
        email = json.load(f).get("client_email")
    print(f"\nService account              {email}")

    from googleapiclient.errors import HttpError

    services = project_docs._get_services()
    drive = services["drive"]

    # 1. Shared Drives the service account can see.
    try:
        drives = drive.drives().list(pageSize=20, fields="drives(id,name)").execute()
    except HttpError as exc:
        fail(f"Could not list Shared Drives: {exc}", "Check the Drive API is enabled.")

    found = drives.get("drives", [])
    print(f"Shared Drives it can reach   {len(found)}")
    for d in found:
        print(f"   - {d['name']}  ({d['id']})")

    if not found:
        fail(
            "The service account is not a member of any Shared Drive.",
            f"A service account has no storage of its own, so it cannot create a\n"
            f"        doc in an ordinary folder — the file would need an owner and it\n"
            f"        cannot be one. Files in a Shared Drive are owned by the drive.\n"
            f"        Create a Shared Drive, then add {email}\n"
            f"        as a Content manager, and put its id in GOOGLE_DRIVE_FOLDER_ID.",
        )

    if not project_docs.FOLDER_ID:
        fail(
            "GOOGLE_DRIVE_FOLDER_ID is not set.",
            f"Use one of the ids above — or a folder inside it.",
        )

    # 2. Is the configured target reachable and writable?
    try:
        target = drive.files().get(
            fileId=project_docs.FOLDER_ID,
            fields="id,name,mimeType,capabilities(canAddChildren)",
            supportsAllDrives=True,
        ).execute()
    except HttpError:
        try:
            target = drive.drives().get(driveId=project_docs.FOLDER_ID,
                                        fields="id,name").execute()
            target["mimeType"] = "shared drive"
            target["capabilities"] = {"canAddChildren": True}
        except HttpError as exc:
            fail(
                f"Cannot reach {project_docs.FOLDER_ID}: {exc}",
                f"Make sure {email} is a member of that Shared Drive.",
            )

    print(f"Target                       {target.get('name')} ({target.get('mimeType')})")
    if not target.get("capabilities", {}).get("canAddChildren", False):
        fail(
            "The service account cannot create files there.",
            f"Give {email} Content manager access, not Viewer or Commenter.",
        )

    # 3. Docs API — separate from Drive, and enabled separately.
    try:
        services["docs"].documents().get(documentId="probe-not-a-real-id").execute()
    except HttpError as exc:
        detail = str(exc)
        if "has not been used in project" in detail or "is disabled" in detail:
            fail(
                "The Google Docs API is not enabled.",
                "Enable it in the Cloud console under APIs & Services → Library →\n"
                "        Google Docs API, wait a minute, and re-run.",
            )
        # A 404 for a made-up id is exactly what a working API returns.
        print("Docs API                     enabled")

    if "--write" not in sys.argv:
        print("\n  OK  Everything needed is in place.")
        print("      To prove writing works, run:")
        print("        .venv/bin/python check_docs.py --write")
        return

    print("\nCreating a test doc…")
    created = project_docs.create(
        "PREFLIGHT TEST — delete me",
        "This doc was created by check_docs.py to prove write access.\n\n"
        + project_docs.MARKER + "\n",
    )
    if not created:
        fail("create() returned nothing.", "Docs are not configured.")
    print(f"\n  OK  Created and written: {created['url']}")
    print("      Delete it yourself — this script never deletes anything.")


if __name__ == "__main__":
    main()
