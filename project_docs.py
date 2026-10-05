"""One Google Doc per project: a collaboration board the bot keeps current.

The doc has two halves, separated by a marker line:

  * above it, a section the bot owns and rewrites on every change — the intake
    answers, the checklist state, the sign-offs, links back to Slack and the
    sheet. Always correct, never worth editing by hand because it gets replaced.
  * below it, the team's own space. Notes, decisions, arguments, links. The bot
    never reads or touches anything down there.

That split is deliberate. There are no content-change webhooks for Google Docs,
so a doc cannot safely drive anything: someone's sentence must not be able to
tick an audit box. Structured state flows bot -> doc, and prose stays where
people put it.

Docs must live in a **Shared Drive**. A service account has no storage quota of
its own (verified: limit 0), so it cannot own a file — but files in a Shared
Drive are owned by the drive, not the creator, which sidesteps that entirely.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")
CREDS_PATH = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]

# Everything above this line belongs to the bot and is replaced wholesale.
# Everything below belongs to the team and is never touched.
MARKER = "───── everything below is yours; the bot never edits it ─────"

_lock = threading.Lock()
_services: Optional[Dict[str, Any]] = None


def enabled() -> bool:
    # The key file has to exist, not just be configured — a missing key would
    # otherwise turn every write into a failure that DMs whoever submitted.
    return bool(FOLDER_ID and CREDS_PATH and os.path.exists(CREDS_PATH))


def _get_services() -> Dict[str, Any]:
    global _services
    if _services is not None:
        return _services

    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_service_account_file(CREDS_PATH, scopes=SCOPES)
    _services = {
        "docs": build("docs", "v1", credentials=creds, cache_discovery=False),
        "drive": build("drive", "v3", credentials=creds, cache_discovery=False),
        "email": creds.service_account_email,
    }
    return _services


# --------------------------------------------------------------------------
# what the bot writes
# --------------------------------------------------------------------------

def _pretty(field: Dict[str, Any], value: Any) -> str:
    options = {o["value"]: o["label"] for o in field.get("options", [])}
    if value in (None, "", []):
        return "—"
    if isinstance(value, list):
        return ", ".join(options.get(v, v) for v in value)
    return options.get(value, str(value))


def managed_text(run: Dict[str, Any], schema: Dict[str, Any], sheet_url: Optional[str],
                 thread_url: Optional[str], now: str) -> str:
    """The bot-owned half of the doc, rendered as plain text.

    Plain text rather than rich formatting: this block is deleted and rewritten
    on every change, and styling ranges would have to be recomputed each time
    for no real gain. Headings people care about live in their own section.
    """
    from form_builder import is_input, visible_fields

    record = run.get("record") or {}
    state = run.get("state") or {}
    lines: List[str] = []

    type_label = schema["project_types"].get(run["project_type"], {}).get(
        "label", run["project_type"]
    )
    lines.append(f"{run['project_name']}")
    lines.append(f"{type_label} · updated {now}")
    lines.append("")

    closed = state.get("closed")
    if closed:
        lines.append(f"CLOSED — {closed['reason']}")
        if closed.get("note"):
            lines.append(f"  {closed['note']}")
        lines.append("")

    owners = run.get("owners") or {}
    if owners:
        lines.append("Owners")
        for key, label in (("research_owner", "Research"), ("delivery_owner", "Delivery"),
                           ("sales_owner", "Sales")):
            if owners.get(key):
                lines.append(f"  {label}: {owners[key]}")
        lines.append("")

    ing = state.get("ingestion")
    if ing:
        import ingestion

        lines.append("Opportunity ingestion")
        decision = ing.get("decision") or {}
        if decision.get("value") == "go":
            lines.append(f"  Go — {decision['at'][:10]}")
        elif decision.get("value") == "no_go":
            lines.append(f"  No-go — {decision['at'][:10]}: {decision.get('note', '')}")
        else:
            lines.append(f"  Awaiting go/no-go (due {ingestion.deadline(state):%d %b %H:%M} UTC)")
        for element in ingestion.ELEMENTS:
            signed = ing["signed"].get(element["key"])
            mark = f"[x] signed {signed['at'][:10]}" if signed else "[ ]"
            lines.append(f"    {mark} {element['label']}")
        for role, label in ingestion.SIGNERS.items():
            done = ing["signoffs"].get(role)
            if done:
                lines.append(f"    {label} signed off {done['at'][:10]}")
        lines.append("")

    # The intake answers, in the order the form asked them.
    fields = [f for f in visible_fields(schema, run["project_type"], record)]
    lines.append("Intake")
    for field in fields:
        if not is_input(field):
            lines.append("")
            lines.append(f"  {field['label']}")
            continue
        value = _pretty(field, record.get(field["key"]))
        if value == "—":
            continue
        if len(value) > 70:
            lines.append(f"    {field['label']}:")
            lines.append(f"      {value}")
        else:
            lines.append(f"    {field['label']}: {value}")
    lines.append("")

    checked = state.get("checked") or {}
    if checked or state.get("signoffs"):
        import gates

        lines.append("Checklist")
        lines.append(f"  {gates.status_line(state)}")
        for step in gates.STEPS:
            done = set(checked.get(step["key"], []))
            lines.append(f"    Step {step['number']}. {step['title']}")
            for item in step["items"]:
                mark = "x" if item["key"] in done else " "
                lines.append(f"      [{mark}] {item['text']}")
            for owner_key in step.get("signoffs", []):
                record_ = (state.get("signoffs") or {}).get(f"{step['key']}:{owner_key}")
                if record_:
                    lines.append(f"      signed off by {record_['user']}")
        lines.append("")

    links = []
    if thread_url:
        links.append(f"  Slack thread: {thread_url}")
    if sheet_url:
        links.append(f"  Tracking sheet: {sheet_url}")
    if links:
        lines.append("Links")
        lines.extend(links)
        lines.append("")

    lines.append(MARKER)
    lines.append("")
    return "\n".join(lines)


STARTER_SECTIONS = (
    "\nNotes\n\n\nDecisions\n\n\nOpen questions\n\n\n"
)


# --------------------------------------------------------------------------
# creating and updating
# --------------------------------------------------------------------------

def create(project_name: str, initial_text: str) -> Optional[Dict[str, str]]:
    """Make the doc in the Shared Drive. Returns its id and url."""
    if not enabled():
        logger.info("Docs disabled (GOOGLE_DRIVE_FOLDER_ID unset)")
        return None

    with _lock:
        services = _get_services()
        # Created through Drive rather than the Docs API so it can be placed in
        # the Shared Drive directly; documents.create has no parent parameter and
        # would land in the service account's own Drive, which has no quota.
        created = services["drive"].files().create(
            body={
                "name": project_name,
                "mimeType": "application/vnd.google-apps.document",
                "parents": [FOLDER_ID],
            },
            fields="id,webViewLink",
            supportsAllDrives=True,
        ).execute()

        doc_id = created["id"]
        services["docs"].documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [
                {"insertText": {"location": {"index": 1},
                                "text": initial_text + STARTER_SECTIONS}}
            ]},
        ).execute()

    return {"id": doc_id, "url": created["webViewLink"]}


def _managed_range(document: Dict[str, Any]) -> Optional[tuple]:
    """Where the bot's half starts and ends, or None if the marker is gone.

    A missing marker means someone deleted it. Rewriting the whole document
    would then destroy the team's notes, so the safe answer is to do nothing and
    say so.
    """
    content = document.get("body", {}).get("content", [])
    for element in content:
        paragraph = element.get("paragraph")
        if not paragraph:
            continue
        text = "".join(
            run.get("textRun", {}).get("content", "") for run in paragraph.get("elements", [])
        )
        if MARKER in text:
            # Through the marker's own paragraph, not up to it. The replacement
            # text carries its own marker, so stopping short would leave the old
            # one behind and the doc would grow a marker per update.
            return 1, element["endIndex"]
    return None


def update(doc_id: str, managed: str) -> bool:
    """Replace the bot-owned half, leaving everything below the marker alone."""
    if not enabled() or not doc_id:
        return False

    with _lock:
        services = _get_services()
        document = services["docs"].documents().get(documentId=doc_id).execute()
        span = _managed_range(document)
        if span is None:
            logger.warning(
                "Doc %s has no bot marker; leaving it alone rather than risk "
                "overwriting the team's notes", doc_id
            )
            return False

        start, end = span
        requests = []
        if end > start:
            requests.append(
                {"deleteContentRange": {"range": {"startIndex": start, "endIndex": end}}}
            )
        requests.append({"insertText": {"location": {"index": start}, "text": managed}})
        services["docs"].documents().batchUpdate(
            documentId=doc_id, body={"requests": requests}
        ).execute()

    return True


def share_with_domain(doc_id: str, domain: str) -> bool:
    """Let everyone in the workspace edit it, so nobody has to request access."""
    if not enabled():
        return False
    try:
        with _lock:
            _get_services()["drive"].permissions().create(
                fileId=doc_id,
                body={"type": "domain", "role": "writer", "domain": domain},
                supportsAllDrives=True,
                sendNotificationEmail=False,
            ).execute()
        return True
    except Exception:
        logger.warning("Could not share doc %s with %s", doc_id, domain, exc_info=True)
        return False
