"""The project doc: created in a Shared Drive, and only ever half-rewritten.

The critical property is containment. The bot replaces its own section on every
change; the team's notes below the marker must survive that, every time. A bug
here silently eats work people typed, which is far worse than a missing doc.

Run: python test_docs.py
"""

import os
import tempfile

os.environ["GOOGLE_DRIVE_FOLDER_ID"] = "SHARED_DRIVE_TEST"
os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = __file__
os.environ["GATES_DB"] = os.path.join(tempfile.mkdtemp(), "docs_test.db")

import gates  # noqa: E402
import project_docs  # noqa: E402
from form_builder import TYPE_KEY, load_schema  # noqa: E402

SCHEMA = load_schema()
failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail and not cond else ''}")
    if not cond:
        failures.append(name)


# --------------------------------------------------------------------------
# a Google Docs document, faithfully enough to exercise the index arithmetic
# --------------------------------------------------------------------------

class FakeDoc:
    """Text plus the paragraph index model the Docs API exposes."""

    def __init__(self):
        self.text = "\n"

    def structure(self):
        """Paragraphs with 1-based start indices, as documents.get returns them."""
        content, index = [], 1
        for line in self.text.split("\n"):
            piece = line + "\n"
            content.append({
                "startIndex": index,
                "endIndex": index + len(piece),
                "paragraph": {"elements": [{"textRun": {"content": piece}}]},
            })
            index += len(piece)
        return {"body": {"content": content}}

    def apply(self, requests):
        for request in requests:
            if "insertText" in request:
                at = request["insertText"]["location"]["index"] - 1
                body = request["insertText"]["text"]
                self.text = self.text[:at] + body + self.text[at:]
            elif "deleteContentRange" in request:
                r = request["deleteContentRange"]["range"]
                self.text = self.text[: r["startIndex"] - 1] + self.text[r["endIndex"] - 1 :]


class FakeDocsService:
    def __init__(self, store):
        self.store = store

    def documents(self):
        return self

    def get(self, documentId):
        return type("R", (), {"execute": lambda _: self.store[documentId].structure()})()

    def batchUpdate(self, documentId, body):
        doc = self.store[documentId]
        return type("R", (), {"execute": lambda _: doc.apply(body["requests"]) or {}})()


class FakeDriveService:
    def __init__(self, store):
        self.store = store
        self.created = []
        self.permissions_set = []

    def files(self):
        return self

    def permissions(self):
        return self

    def create(self, body=None, fields=None, supportsAllDrives=None, sendNotificationEmail=None,
               fileId=None):
        if fileId is not None:  # permissions().create
            self.permissions_set.append((fileId, body))
            return type("R", (), {"execute": lambda _: {}})()
        self.created.append({"body": body, "supportsAllDrives": supportsAllDrives})
        doc_id = f"doc_{len(self.created)}"
        self.store[doc_id] = FakeDoc()
        return type("R", (), {"execute": lambda _: {
            "id": doc_id, "webViewLink": f"https://docs.google.com/document/d/{doc_id}/edit"}})()


store = {}
drive = FakeDriveService(store)
project_docs._services = {"docs": FakeDocsService(store), "drive": drive, "email": "sa@test"}

check("docs enabled once a folder is configured", project_docs.enabled())

# --------------------------------------------------------------------------
# creation
# --------------------------------------------------------------------------
gates.init_db()
run_id = gates.create_run("C1", "170.5", "Attack Bench", "sample",
                          {"research_owner": "U_RES", "delivery_owner": "U_DEL"},
                          record={TYPE_KEY: "sample", "project_name": "Attack Bench",
                                  "account": "Tencent", "urgency": "critical",
                                  "intake_stage": "underway",
                                  "failure_pattern": "context drift across turns"})
run = gates.get_run(run_id)

managed = project_docs.managed_text(run, SCHEMA, "https://sheet", "https://slack", "17 Aug, 12:00")
created = project_docs.create("Attack Bench", managed)

check("doc created", created is not None and created["id"] == "doc_1")
check("created in the Shared Drive", drive.created[0]["body"]["parents"] == ["SHARED_DRIVE_TEST"])
check("created with Shared Drive support on", drive.created[0]["supportsAllDrives"] is True,
      "without this the API refuses to write to a Shared Drive")
check("created as a Google Doc",
      drive.created[0]["body"]["mimeType"] == "application/vnd.google-apps.document")

body = store["doc_1"].text
check("intake answers are in the doc", "Tencent" in body and "context drift across turns" in body)
check("the marker is present", project_docs.MARKER in body)
check("starter sections for the team", "Decisions" in body and "Open questions" in body)

# --------------------------------------------------------------------------
# the property that matters: the team's notes survive every rewrite
# --------------------------------------------------------------------------
NOTES = "Kicked off with Tencent 14 Aug. They care most about multi-turn refusals."
store["doc_1"].text += f"\n{NOTES}\nDecision: use ARGO taxonomy as the base.\n"

gates.set_doc(run_id, "doc_1", created["url"])
gates.set_checked(run_id, "seed", ["fidelity", "surfaces"])
run = gates.get_run(run_id)

for round_number in range(1, 4):
    fresh = project_docs.managed_text(run, SCHEMA, "https://sheet", "https://slack",
                                      f"17 Aug, 12:0{round_number}")
    check(f"update {round_number} applied", project_docs.update("doc_1", fresh))
    body = store["doc_1"].text
    check(f"notes survive update {round_number}", NOTES in body)
    check(f"decision survives update {round_number}", "use ARGO taxonomy" in body)
    check(f"only one marker after update {round_number}", body.count(project_docs.MARKER) == 1,
          str(body.count(project_docs.MARKER)))
    check(f"no stale copy of the managed half after {round_number}",
          body.count("Attack Bench") <= 2, f"{body.count('Attack Bench')} copies")

check("gate progress reached the doc", "[x] Seed samples built at full shipping fidelity"
      in store["doc_1"].text)
check("unticked items show unticked", "[ ] Reference sample written" in store["doc_1"].text)

# --------------------------------------------------------------------------
# a deleted marker must stop the bot, not license it to rewrite everything
# --------------------------------------------------------------------------
store["doc_2"] = FakeDoc()
store["doc_2"].text = "Someone removed the marker\nbut kept lots of valuable notes here.\n"
before = store["doc_2"].text
check("refuses to update a doc with no marker", not project_docs.update("doc_2", "new"))
check("that doc is left completely untouched", store["doc_2"].text == before)

# --------------------------------------------------------------------------
# sharing, and graceful degradation
# --------------------------------------------------------------------------
check("shared with the domain", project_docs.share_with_domain("doc_1", "teamdeccan.com"))
check("shared as an editor", drive.permissions_set[0][1]["role"] == "writer")
check("shared with the right domain", drive.permissions_set[0][1]["domain"] == "teamdeccan.com")

project_docs.FOLDER_ID = None
check("disabled without a folder", not project_docs.enabled())
check("create is a no-op when disabled", project_docs.create("x", "y") is None)
check("update is a no-op when disabled", not project_docs.update("doc_1", "z"))

print()
print(f"{len(failures)} failure(s)" if failures else "all checks passed")
raise SystemExit(1 if failures else 0)
