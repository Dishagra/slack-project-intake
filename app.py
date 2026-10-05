"""Dynamic project-intake bot for Slack (Socket Mode).

Flow:
  /new-project                -> modal with just the project-type picker
  pick a type                 -> views.update swaps in that type's whole form
  change a controlling field  -> views.update shows/hides dependent fields
  submit                      -> channel summary + Google Sheet row

Answers are accumulated per open modal, because Slack reports state only for
blocks it is currently rendering: hiding a field would otherwise erase what was
typed into it, with nothing left to restore when it comes back.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import gates
import ingestion
import ingestion_blocks
import project_docs
import sheets
import watchdog
from admin_views import (
    CLOSE_CALLBACK,
    EDIT_CALLBACK,
    OWNERS_CALLBACK,
    close_view,
    detail_view,
    owners_view,
    summary_blocks,
)
from gate_blocks import (
    CHECK_ACTION,
    CLOSE_ACTION,
    OWNERS_ACTION,
    REOPEN_ACTION,
    RETURN_ACTION,
    SIGNOFF_ACTION,
    checklist_blocks,
)
from form_builder import (
    CALLBACK_ID,
    TYPE_KEY,
    all_fields,
    build_view,
    extract_state,
    is_input,
    is_required,
    load_schema,
    trigger_keys,
    visible_fields,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
# Only this module gets turned up; slack_bolt at DEBUG buries everything else.
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

SCHEMA = load_schema()
COMMON_KEYS = {f["key"] for f in SCHEMA.get("common_fields", [])}

# Owners get @-mentioned in the channel summary so the right people see it land.
OWNER_KEYS = ["requestor", "research_owner", "delivery_owner", "sales_owner"]

# Slack expires a trigger_id three seconds after the click, so a listener that
# is still queued when its turn comes can no longer open a modal. Bolt's default
# pool is five threads; a handful of quick ticks was enough to starve it.
app = App(
    token=os.environ["SLACK_BOT_TOKEN"],
    listener_executor=ThreadPoolExecutor(max_workers=20, thread_name_prefix="listener"),
)

# Sheet writes are three round-trips to Google and nobody is waiting on them, so
# they run off the interactive path. One worker keeps them in order, and the
# module's own lock still guards the read/modify/write.
_sheet_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sheets")


def _write_sheet_later(fn, *args) -> None:
    def run():
        try:
            fn(*args)
        except Exception:
            logger.exception("Deferred sheet write failed")

    _sheet_writer.submit(run)


def _thread_url(run: Dict[str, Any]) -> Optional[str]:
    """Permalink to the intake message, so the doc can point back at Slack."""
    team = os.environ.get("SLACK_TEAM_DOMAIN")
    if not (team and run.get("channel") and run.get("thread_ts")):
        return None
    return (
        f"https://{team}.slack.com/archives/{run['channel']}/"
        f"p{run['thread_ts'].replace('.', '')}"
    )


def _push_doc(run_id: str) -> None:
    """Rewrite the bot-owned half of the project doc, off the interactive path."""
    if not project_docs.enabled():
        return

    def write():
        run = gates.get_run(run_id)
        if not run or not run.get("doc_id"):
            return
        now = dt.datetime.now().strftime("%d %b %Y, %H:%M")
        project_docs.update(
            run["doc_id"],
            project_docs.managed_text(
                run, SCHEMA, sheets.sheet_url(), _thread_url(run), now
            ),
        )

    _write_sheet_later(write)


def _push_status(run_id: str) -> None:
    """Queue a status write that reads the run at write time, not at click time.

    Two people ticking a second apart would otherwise race: whichever HTTP call
    finishes last wins, and the sheet can end up showing the earlier state. The
    worker reading the run itself means the sheet converges on the truth however
    the writes interleave.
    """

    def write():
        run = gates.get_run(run_id)
        if run:
            sheets.update_status(run_id, gates.status_line(run["state"]))

    _write_sheet_later(write)


# Answers typed into the open modal, keyed by Slack's view id. Slack reports
# state only for rendered blocks, so this is the only place a hidden field's
# answer survives. Entries are dropped when the modal closes; the cap is a
# backstop against a leak if a close is ever missed.
_MAX_TRACKED_VIEWS = 500
_answers: Dict[str, Dict[str, Any]] = {}
_answers_lock = threading.Lock()


def _remember(view_id: str, live: Dict[str, Any]) -> Dict[str, Any]:
    """Merge this render's answers over what the modal has collected so far."""
    with _answers_lock:
        merged = {**_answers.get(view_id, {}), **live}
        if view_id not in _answers and len(_answers) >= _MAX_TRACKED_VIEWS:
            _answers.pop(next(iter(_answers)), None)
        _answers[view_id] = merged
        return dict(merged)


def _forget(view_id: str) -> None:
    with _answers_lock:
        _answers.pop(view_id, None)


# Every click and submission, logged as it arrives. Without this, "the user didn't
# click" and "the click never reached us" look identical — which is exactly the
# question that matters when a button seems to do nothing.
@app.middleware
def log_interaction(body, next, logger):
    kind = body.get("type") or ("command" if body.get("command") else "event")
    user = (body.get("user") or {}).get("id") or body.get("user_id")
    if kind == "block_actions":
        what = ",".join(a.get("action_id", "?") for a in body.get("actions", []))
    elif kind in ("view_submission", "view_closed"):
        what = (body.get("view") or {}).get("callback_id", "?")
    else:
        what = body.get("command") or kind
    logging.getLogger("interactions").info("%s %s by %s", kind, what, user)
    return next()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _label_for(field: Dict[str, Any], value: Any) -> str:
    """Human-readable value: option labels instead of raw option values."""
    options = {o["value"]: o["label"] for o in field.get("options", [])}
    if field["type"] in ("user",) and value:
        return f"<@{value}>"
    if field["type"] in ("multi_user",) and value:
        return ", ".join(f"<@{v}>" for v in value)
    if isinstance(value, list):
        return ", ".join(options.get(v, v) for v in value)
    return options.get(value, str(value)) if value is not None else "—"


class ViewGone(Exception):
    """The modal is no longer open. Nothing to update, and nothing is wrong."""


def _layout(blocks) -> tuple:
    """What a modal looks like, ignoring the values typed into it.

    Two renders with the same blocks in the same order are the same form; only
    the answers differ, and the client already has those.
    """
    return tuple(
        (b.get("block_id"), b.get("type"), b.get("element", {}).get("type"), b.get("optional"))
        for b in blocks
    )


def _rerender(client, body: Dict[str, Any], project_type, state: Dict[str, Any]) -> None:
    view = body["view"]
    # Carry the callback through: the same form serves a new intake and an edit,
    # and rebuilding with the default would silently turn a save into a new
    # submission the moment the editor touched any dropdown.
    callback_id = view.get("callback_id") or CALLBACK_ID
    new_view = build_view(
        SCHEMA,
        project_type,
        state,
        view.get("private_metadata", ""),
        callback_id=callback_id,
        submit_label="Save changes" if callback_id == EDIT_CALLBACK else None,
    )

    # Roughly two in five trigger changes reveal and hide nothing — picking High
    # instead of Medium leaves the same fields on screen. Pushing an identical
    # form back would still rebuild the modal in the client, which throws away
    # keyboard focus: start typing in the next field and the cursor jumps away.
    # Nothing needs sending when nothing moved.
    if _layout(view.get("blocks", [])) == _layout(new_view["blocks"]):
        logger.debug("SKIPPED re-render — form unchanged, focus preserved")
        return
    logger.debug(
        "RE-RENDER sent — %d fields before, %d after",
        sum(1 for b in view.get("blocks", []) if b.get("type") == "input"),
        sum(1 for b in new_view["blocks"] if b.get("type") == "input"),
    )

    try:
        client.views_update(view_id=view["id"], hash=view["hash"], view=new_view)
        return
    except Exception as e:
        message = str(e)
        # The user closed or submitted the modal before this event was handled.
        # Retrying cannot succeed and there is nobody left to show it to.
        if "not_found" in message:
            raise ViewGone from e
        # hash_conflict means they changed something else mid-flight; the newer
        # view already reflects it, so retry unguarded rather than drop.
        if "hash_conflict" not in message:
            raise
    try:
        client.views_update(view_id=view["id"], view=new_view)
    except Exception as e:
        if "not_found" in str(e):
            raise ViewGone from e
        raise


def _rerender_or_recover(client, body: Dict[str, Any], project_type, state: Dict[str, Any]) -> None:
    """Re-render, and if Slack rejects the view, get the user a working form back.

    A rejected views.update leaves the modal showing "We had some trouble
    connecting", with no way forward. Retrying without the answers Slack
    objected to costs the user some typing; leaving the modal dead costs them
    all of it.
    """
    try:
        _rerender(client, body, project_type, state)
        return
    except ViewGone:
        logger.debug("Modal already closed; skipping re-render")
        return
    except Exception:
        logger.exception("views.update rejected; retrying with text answers only")

    # Drop only the answers Slack validates, and identify them by their schema
    # type rather than by how the text looks. A prefix test both missed the real
    # offenders and deleted ordinary prose like "https and grpc both need wiring".
    strict = {
        f["key"]
        for f in all_fields(SCHEMA, project_type)
        if f["type"] in ("url", "email")
    }
    safe = {k: v for k, v in state.items() if k not in strict}
    try:
        _rerender(client, body, project_type, safe)
    except ViewGone:
        logger.debug("Modal already closed; skipping re-render")
    except Exception:
        logger.exception("views.update rejected again; leaving the modal as it was")


# --------------------------------------------------------------------------
# open
# --------------------------------------------------------------------------

@app.command("/new-project")
def open_modal(ack, body, client):
    # Slack gives three seconds from issuing the trigger to opening the modal,
    # and reports "the app did not respond" if the ack misses that. Timing each
    # step is the only way to tell a slow handler from an event that was already
    # stale when it arrived — they look identical from Slack's side.
    started = time.monotonic()
    ack()
    acked = time.monotonic()

    metadata = json.dumps({"channel_id": body.get("channel_id")})
    view = build_view(SCHEMA, None, {}, metadata)
    built = time.monotonic()

    try:
        client.views_open(trigger_id=body["trigger_id"], view=view)
    except Exception as e:
        logger.error(
            "views.open failed after %.2fs (ack %.2fs, build %.2fs): %s",
            time.monotonic() - started, acked - started, built - acked, e,
        )
        raise
    logger.info(
        "/new-project opened in %.2fs (ack %.2fs, build %.2fs, open %.2fs)",
        time.monotonic() - started, acked - started, built - acked,
        time.monotonic() - built,
    )


@app.shortcut("new_project_intake")
def open_modal_shortcut(ack, body, client):
    ack()
    channel_id = (body.get("channel") or {}).get("id")
    client.views_open(
        trigger_id=body["trigger_id"],
        view=build_view(SCHEMA, None, {}, json.dumps({"channel_id": channel_id})),
    )


# --------------------------------------------------------------------------
# re-render
# --------------------------------------------------------------------------

@app.action(TYPE_KEY)
def on_type_change(ack, body, client):
    """Switching type keeps any answer the new form also asks for.

    The types overlap more than they look: pilot and production share ten
    identically-labelled questions with identical options. Blanking those made
    the bot ask the same question twice and throw away the first answer.
    """
    ack()
    view_id = body["view"]["id"]
    state = _remember(view_id, extract_state(body["view"]))
    new_type = state.get(TYPE_KEY)

    wanted = COMMON_KEYS | {f["key"] for f in all_fields(SCHEMA, new_type)}
    carried = {k: v for k, v in state.items() if k in wanted or k == TYPE_KEY}
    with _answers_lock:
        _answers[view_id] = dict(carried)

    _rerender_or_recover(client, body, new_type, carried)


def _on_trigger_change(ack, body, client):
    ack()
    state = _remember(body["view"]["id"], extract_state(body["view"]))
    _rerender_or_recover(client, body, state.get(TYPE_KEY), state)


for key in trigger_keys(SCHEMA) - {TYPE_KEY}:
    app.action(key)(_on_trigger_change)


# --------------------------------------------------------------------------
# submit
# --------------------------------------------------------------------------

@app.view(CALLBACK_ID)
def on_submit(ack, body, client, view, logger):
    view_id = view["id"]
    # What is on screen wins; anything hidden falls back to what was typed earlier.
    state = {**_answers.get(view_id, {}), **extract_state(view)}
    project_type = state.get(TYPE_KEY)
    # Section headers render as context blocks; they hold no value to validate or
    # store. Only what the submitted form actually showed gets recorded, so an
    # answer that was later hidden is remembered for the modal but never filed.
    fields = [f for f in visible_fields(SCHEMA, project_type, state) if is_input(f)]

    rendered = {b.get("block_id") for b in view["blocks"]}
    missing = {
        f["key"]: "This field is required"
        for f in fields
        if is_required(f, state)
        and state.get(f["key"]) in (None, "", [])
        # Slack discards an errors response naming a block it is not showing,
        # which would silently swallow the whole rejection.
        and f["key"] in rendered
    }
    if missing:
        ack(response_action="errors", errors=missing)
        return

    ack()
    _forget(view_id)

    try:
        _record_submission(body, client, view, state, project_type, fields)
    except Exception:
        logger.exception("Intake submission failed after ack")
        client.chat_postMessage(
            channel=body["user"]["id"],
            text=(
                "Something went wrong filing your intake after you hit Submit. "
                "Nothing reliable was recorded — please raise it again, and tell "
                "whoever runs the bot."
            ),
        )


def _record_submission(body, client, view, state, project_type, fields) -> None:

    user_id = body["user"]["id"]
    metadata = json.loads(view.get("private_metadata") or "{}")
    channel_id = metadata.get("channel_id")
    type_label = SCHEMA["project_types"][project_type]["label"]

    record: Dict[str, Any] = {
        "submitted_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "submitted_by": body["user"].get("username") or user_id,
        "project_type": type_label,
    }
    for f in fields:
        record[f["key"]] = state.get(f["key"])

    sheet_url = sheets.sheet_url() if sheets.enabled() else None
    project_name = state.get("project_name", "Untitled")
    target = channel_id or user_id

    # Every intake gets a run so its detail stays retrievable; only gated types
    # also get a checklist. The run exists before the message because the
    # summary's button carries its id.
    owners = {k: state[k] for k in OWNER_KEYS if state.get(k)}
    run_id = gates.create_run(target, "", project_name, project_type, owners, record=state)
    record[sheets.RUN_ID_COL] = run_id
    record[sheets.STATUS_COL] = (
        gates.status_line(gates.get_run(run_id)["state"])
        if project_type in gates.GATED_TYPES
        else "No checklist for this type"
    )

    # Created before the sheet row so the link goes in with everything else, and
    # before the summary so the post can carry a button straight to it.
    doc_url = None
    if project_docs.enabled():
        try:
            run = gates.get_run(run_id)
            created = project_docs.create(
                project_name,
                project_docs.managed_text(run, SCHEMA, sheet_url, None, 
                                          dt.datetime.now().strftime("%d %b %Y, %H:%M")),
            )
            if created:
                gates.set_doc(run_id, created["id"], created["url"])
                doc_url = created["url"]
                record[sheets.DOC_COL] = doc_url
                domain = os.environ.get("GOOGLE_SHARE_DOMAIN", "").strip()
                if domain:
                    project_docs.share_with_domain(created["id"], domain)
        except Exception:
            # A missing doc is a degraded intake, not a failed one.
            logger.exception("Could not create the project doc for %s", run_id)

    blocks = summary_blocks(
        run_id, project_name, type_label, user_id, state, OWNER_KEYS, sheet_url,
        doc_url=doc_url,
    )
    fallback = f"New {type_label} project: {project_name}"
    try:
        posted = client.chat_postMessage(channel=target, text=fallback, blocks=blocks)
    except Exception:
        logger.exception("Channel post failed; falling back to DM")
        posted = client.chat_postMessage(channel=user_id, text=fallback, blocks=blocks)

    # The response carries the real conversation id. A user id works only for
    # chat.postMessage, which opens the IM implicitly; chat.update and
    # chat.postEphemeral need the D… id, so storing U… here would freeze the
    # checklist and silently swallow every sign-off rejection.
    target = posted["channel"]
    gates.set_thread(run_id, target, posted["ts"])

    def write_row():
        try:
            sheets.append_row(record)
        except Exception:
            logger.exception("Google Sheets write failed")
            client.chat_postMessage(
                channel=user_id,
                text=(
                    "Your intake posted to the channel, but writing it to the tracking "
                    "sheet failed. The data is in the channel message and not lost — "
                    "ping whoever runs the bot."
                ),
            )

    # Queued ahead of any status update for this run, so the row exists before
    # anything tries to find it.
    _sheet_writer.submit(write_row)

    if ingestion.needed(state):
        # Delivery accepts the work before anything else happens. The Steps 2-6
        # checklist is posted once this is signed off, not now.
        _post_ingestion(client, run_id, user_id)
    else:
        _post_checklist(client, run_id, user_id)


def _post_ingestion(client, run_id: str, user_id: str) -> None:
    ingestion.start(run_id, gates.get_run(run_id).get("record") or {})
    run = gates.get_run(run_id)
    try:
        posted = client.chat_postMessage(
            channel=run["channel"],
            thread_ts=run["thread_ts"],
            text=f"Opportunity ingestion — {run['project_name']}",
            blocks=ingestion_blocks.blocks(run),
        )
        ingestion.set_message_ts(run_id, posted["ts"])
    except Exception:
        logger.exception("Ingestion post failed for run %s", run_id)
        client.chat_postMessage(
            channel=user_id,
            text=(f"*{run['project_name']}* was filed, but its ingestion checklist could "
                  f"not be posted, so Delivery's 36-hour clock is not being tracked. "
                  f"Reference `{run_id}` when you report this."),
        )
        return
    owners = run["owners"]
    if owners.get("delivery_owner"):
        client.chat_postMessage(
            channel=run["channel"], thread_ts=run["thread_ts"],
            text=(f"<@{owners['delivery_owner']}> — new opportunity for Delivery. Sign off the "
                  f"elements above and make the go/no-go call within "
                  f"{ingestion.SLA_HOURS} hours."),
        )


def _post_checklist(client, run_id: str, user_id: str) -> None:
    """The Steps 2-6 checklist, for gated types once ingestion allows it."""
    run = gates.get_run(run_id)
    if run["project_type"] not in gates.GATED_TYPES or run.get("message_ts"):
        return
    try:
        checklist = client.chat_postMessage(
            channel=run["channel"],
            thread_ts=run["thread_ts"],
            text=f"Sample Creation Checklist — {run['project_name']}",
            blocks=checklist_blocks(run),
        )
        gates.set_message_ts(run_id, checklist["ts"])
    except Exception:
        # Without a message_ts the run can never refresh, and the submitter
        # would see a clean summary with no reason to suspect anything.
        logger.exception("Checklist post failed for run %s", run_id)
        client.chat_postMessage(
            channel=user_id,
            text=(
                f"*{run['project_name']}* was filed, but its checklist could not be "
                f"posted, so the Steps 2–6 gates are not being tracked. "
                f"Reference `{run_id}` when you report this."
            ),
        )


GONE_MESSAGE = (
    "This checklist is no longer being tracked — its record is gone from the "
    "bot's database. Raise the project again with `/new-project`."
)


def _run_or_tell(client, body, run_id: str):
    """Fetch a run, or explain to the clicker why the button did nothing.

    Every checklist message ever posted lives in Slack forever, but the database
    behind it does not: a redeploy or a moved directory leaves those buttons
    pointing at rows that no longer exist. Silence is the worst answer here — the
    tick appears to register in the clicker's own client and nothing persists.
    """
    run = gates.get_run(run_id)
    if run is None:
        logger.warning("Interaction for unknown run %s", run_id)
        _tell(client, body, GONE_MESSAGE)
    return run


def _tell(client, body, message: str, run=None) -> None:
    """Ephemeral reply, placed in the thread the button actually lives in."""
    channel = (body.get("channel") or {}).get("id") or (run or {}).get("channel")
    if not channel:
        return
    try:
        client.chat_postEphemeral(
            channel=channel,
            user=body["user"]["id"],
            text=message,
            thread_ts=(run or {}).get("thread_ts"),
        )
    except Exception:
        logger.exception("Could not deliver an ephemeral reply")


def _refresh(client, run_id: str) -> None:
    run = gates.get_run(run_id)
    if not run:
        return
    if run["message_ts"]:
        try:
            client.chat_update(
                channel=run["channel"],
                ts=run["message_ts"],
                text=f"Sample Creation Checklist — {run['project_name']}",
                blocks=checklist_blocks(run),
            )
        except Exception:
            # The state is already committed; a failed repaint must not also
            # stop the sheet from catching up.
            logger.exception("Could not repaint the checklist for run %s", run_id)

    # The sheet is the view for people who are not in the thread, so it is kept
    # current on every interaction — but off the interactive path, because three
    # Google round-trips inside a listener is what starves the pool and expires
    # the next click's trigger_id.
    _push_status(run_id)
    _push_doc(run_id)


def _parse_action(action_id: str, count: int):
    """Split an action id into its fixed number of parts, or None if malformed."""
    parts = action_id.split(":")
    return parts if len(parts) == count else None


# --------------------------------------------------------------------------
# opportunity ingestion
# --------------------------------------------------------------------------

def _refresh_ingestion(client, run_id: str) -> None:
    run = gates.get_run(run_id)
    ing = ingestion.of(run["state"]) if run else None
    if not ing or not ing.get("message_ts"):
        return
    try:
        client.chat_update(
            channel=run["channel"], ts=ing["message_ts"],
            text=f"Opportunity ingestion — {run['project_name']}",
            blocks=ingestion_blocks.blocks(run),
        )
    except Exception:
        logger.exception("Could not repaint the ingestion message for run %s", run_id)
    _push_status(run_id)
    _push_doc(run_id)


def _blank(value) -> bool:
    return value in (None, "", []) or (isinstance(value, str) and not value.strip())


def _say(client, run, text: str) -> None:
    client.chat_postMessage(channel=run["channel"], thread_ts=run["thread_ts"], text=text)


@app.action(re.compile(rf"^{ingestion_blocks.CHECK}:"))
def on_ingest_check(ack, body, action, client):
    ack()
    run_id = action["action_id"].split(":")[1]
    run = _run_or_tell(client, body, run_id)
    if not run:
        return
    ok, message = ingestion.set_signed(
        run_id, [o["value"] for o in action.get("selected_options", [])], body["user"]["id"]
    )
    if not ok:
        _tell(client, body, message, run)
    _refresh_ingestion(client, run_id)


@app.action(re.compile(rf"^{ingestion_blocks.GO}:"))
def on_ingest_go(ack, body, action, client):
    ack()
    run_id = action["action_id"].split(":")[1]
    run = _run_or_tell(client, body, run_id)
    if not run:
        return
    ok, message = ingestion.decide(run_id, "go", body["user"]["id"])
    if not ok:
        _tell(client, body, message, run)
        return
    owners = run["owners"]
    who = " ".join(f"<@{owners[k]}>" for k in ingestion.SIGNERS if owners.get(k))
    _say(client, run, f"✅ Delivery called *go* on {run['project_name']}. {who} — final sign-off "
                      f"from each of you, and work starts.")
    _refresh_ingestion(client, run_id)


@app.action(re.compile(rf"^{ingestion_blocks.NO_GO}:"))
def on_ingest_nogo(ack, body, action, client):
    ack()
    run_id = action["action_id"].split(":")[1]
    run = _run_or_tell(client, body, run_id)
    if run:
        client.views_open(trigger_id=body["trigger_id"], view=ingestion_blocks.no_go_view(run_id))


@app.view(ingestion_blocks.NO_GO_CALLBACK)
def on_ingest_nogo_submitted(ack, body, view, client):
    note = (view["state"]["values"]["text"]["text"].get("value") or "").strip()
    run_id = view["private_metadata"]
    ok, message = ingestion.decide(run_id, "no_go", body["user"]["id"], note)
    if not ok:
        ack(response_action="errors", errors={"text": message})
        return
    ack()
    # No-go ends the opportunity; closing keeps the record and allows a reopen.
    gates.close_run(run_id, body["user"]["id"], "no_go", note)
    run = gates.get_run(run_id)
    requestor = run["owners"].get("requestor")
    _say(client, run, f"⛔ Delivery called *no-go* on {run['project_name']}"
                      + (f" — <@{requestor}>" if requestor else "") + f"\n> {note}")
    _refresh_ingestion(client, run_id)


@app.action(re.compile(rf"^{ingestion_blocks.ASK}:"))
def on_ingest_ask(ack, body, action, client):
    ack()
    run_id = action["action_id"].split(":")[1]
    if _run_or_tell(client, body, run_id):
        client.views_open(trigger_id=body["trigger_id"], view=ingestion_blocks.ask_view(run_id))


@app.view(ingestion_blocks.ASK_CALLBACK)
def on_ingest_ask_submitted(ack, body, view, client):
    ack()
    run_id = view["private_metadata"]
    question = (view["state"]["values"]["text"]["text"].get("value") or "").strip()
    if not question:
        return
    ingestion.ask(run_id, body["user"]["id"], question)
    run = gates.get_run(run_id)
    requestor = run["owners"].get("requestor")
    _say(client, run, f"💬 <@{body['user']['id']}> asks"
                      + (f" <@{requestor}>" if requestor else "") + f":\n> {question}")
    _refresh_ingestion(client, run_id)


@app.action(re.compile(rf"^{ingestion_blocks.SIGN}:"))
def on_ingest_sign(ack, body, action, client):
    ack()
    parts = _parse_action(action["action_id"], 3)
    if not parts:
        return
    _, run_id, role = parts
    run = _run_or_tell(client, body, run_id)
    if not run:
        return
    ok, message, completed = ingestion.sign_off(run_id, role, body["user"]["id"])
    _tell(client, body, message, run)
    _refresh_ingestion(client, run_id)
    if completed:
        _say(client, run, f"✅ Ingestion for *{run['project_name']}* is signed off. Work starts.")
        _post_checklist(client, run_id, body["user"]["id"])


def _ingestion_sweep(client, interval_seconds: int = 600) -> None:
    """Remind Delivery before the 36-hour go/no-go deadline, and when it is missed."""

    def run_forever():
        while True:
            try:
                for run in gates.open_ingestions():
                    for kind in ingestion.due_nudges(run["state"]):
                        owner = run["owners"].get("delivery_owner")
                        tag = f"<@{owner}>" if owner else "Delivery"
                        if kind == "warn":
                            text = (f"🟠 {tag} — go/no-go on *{run['project_name']}* is due in "
                                    f"about {ingestion.SLA_HOURS - ingestion.WARN_HOURS} hours.")
                        else:
                            requestor = run["owners"].get("requestor")
                            text = (f"🔴 {tag} — the {ingestion.SLA_HOURS}-hour go/no-go on "
                                    f"*{run['project_name']}* has passed"
                                    + (f". <@{requestor}>, chase if you need an answer." if requestor else "."))
                        _say(client, run, text)
                        ingestion.record_nudge(run["id"], kind)
                        _refresh_ingestion(client, run["id"])
            except Exception:
                logger.exception("Ingestion sweep failed; will try again")
            time.sleep(interval_seconds)

    threading.Thread(target=run_forever, name="ingestion-sweep", daemon=True).start()


# Action ids carry their run and step: "gate_check:<run_id>:<step_key>".
@app.action(re.compile(rf"^{CHECK_ACTION}:"))
def on_gate_check(ack, body, action, client):
    ack()
    parts = _parse_action(action["action_id"], 3)
    if not parts:
        return
    _, run_id, step_key = parts
    if not _run_or_tell(client, body, run_id):
        return

    ok, refusal, _ = gates.set_checked(
        run_id, step_key, [o["value"] for o in action.get("selected_options", [])]
    )
    if not ok:
        _tell(client, body, refusal, gates.get_run(run_id))
    _refresh(client, run_id)


@app.action(re.compile(rf"^{SIGNOFF_ACTION}:"))
def on_gate_signoff(ack, body, action, client):
    ack()
    parts = _parse_action(action["action_id"], 4)
    if not parts:
        return
    _, run_id, step_key, owner_key = parts
    run = _run_or_tell(client, body, run_id)
    if not run:
        return

    ok, message = gates.sign_off(run_id, step_key, owner_key, body["user"]["id"])
    _tell(client, body, message, run)
    if ok:
        _refresh(client, run_id)


@app.action(re.compile(r"^intake_detail:"))
def on_view_detail(ack, body, action, client):
    ack()
    run = _run_or_tell(client, body, action["action_id"].split(":")[1])
    if run:
        client.views_open(trigger_id=body["trigger_id"], view=detail_view(run))


# The "Open sheet" button is a plain link; acknowledge it so Slack stops asking.
@app.action(re.compile(r"^intake_(sheet|doc):"))
def on_open_sheet(ack):
    ack()


@app.action(re.compile(r"^intake_edit:"))
def on_edit_clicked(ack, body, action, client):
    """Reopen the intake form, filled in with what was submitted."""
    ack()
    run_id = action["action_id"].split(":")[1]
    run = _run_or_tell(client, body, run_id)
    if not run:
        return

    record = run.get("record") or {}
    view = build_view(
        SCHEMA,
        run["project_type"],
        {**record, TYPE_KEY: run["project_type"]},
        json.dumps({"run_id": run_id, "channel_id": run["channel"]}),
        callback_id=EDIT_CALLBACK,
        submit_label="Save changes",
    )
    opened = client.views_open(trigger_id=body["trigger_id"], view=view)
    # Seed the accumulator so fields hidden by a conditional are not lost the
    # moment the editor touches a controller.
    _remember(opened["view"]["id"], {**record, TYPE_KEY: run["project_type"]})


@app.view(EDIT_CALLBACK)
def on_edit_submitted(ack, body, client, view, logger):
    view_id = view["id"]
    state = {**_answers.get(view_id, {}), **extract_state(view)}
    project_type = state.get(TYPE_KEY)
    fields = [f for f in visible_fields(SCHEMA, project_type, state) if is_input(f)]

    rendered = {b.get("block_id") for b in view["blocks"]}
    missing = {
        f["key"]: "This field is required"
        for f in fields
        if is_required(f, state)
        and state.get(f["key"]) in (None, "", [])
        and f["key"] in rendered
    }
    if missing:
        ack(response_action="errors", errors=missing)
        return

    ack()
    _forget(view_id)

    metadata = json.loads(view.get("private_metadata") or "{}")
    run_id = metadata.get("run_id")
    user_id = body["user"]["id"]

    try:
        _apply_edit(client, run_id, state, project_type, fields, user_id)
    except gates.RunGone:
        client.chat_postMessage(channel=user_id, text=GONE_MESSAGE)
    except Exception:
        logger.exception("Edit failed for run %s", run_id)
        client.chat_postMessage(
            channel=user_id,
            text="Your changes could not be saved. Nothing was altered — try again, "
            "and tell whoever runs the bot if it keeps happening.",
        )


def _apply_edit(client, run_id, state, project_type, fields, user_id) -> None:
    run = gates.get_run(run_id)
    if run is None:
        raise gates.RunGone(run_id)

    type_label = SCHEMA["project_types"][project_type]["label"]
    project_name = state.get("project_name", "Untitled")
    was_type = run["project_type"]

    # Only what the edited form actually showed is kept, matching submit.
    record = {f["key"]: state.get(f["key"]) for f in fields}
    record[TYPE_KEY] = project_type

    changes = gates.update_record(run_id, record, project_name, project_type, user_id)

    # Owner fields live in the record, but sign-offs check the owner list. Without
    # this, editing the requestor would show the new name and still accept only
    # the old person's signature.
    wanted = {k: record[k] for k in OWNER_KEYS if record.get(k)}
    if wanted != {k: v for k, v in run["owners"].items() if k in OWNER_KEYS}:
        gates.set_owners(run_id, wanted, user_id)
    if not changes:
        client.chat_postEphemeral(
            channel=run["channel"], user=user_id, thread_ts=run["thread_ts"],
            text="Nothing changed, so nothing was recorded.",
        )
        return

    updated = gates.get_run(run_id)

    # The channel summary is the thread's parent message.
    try:
        client.chat_update(
            channel=updated["channel"],
            ts=updated["thread_ts"],
            text=f"{type_label} project: {project_name}",
            blocks=summary_blocks(
                run_id, project_name, type_label, user_id, state, OWNER_KEYS,
                sheets.sheet_url() if sheets.enabled() else None,
                doc_url=updated.get("doc_url"),
            ),
        )
    except Exception:
        logger.exception("Could not update the summary message for run %s", run_id)

    shown = changes[:10]
    more = f"\n_…and {len(changes) - len(shown)} more._" if len(changes) > len(shown) else ""
    client.chat_postMessage(
        channel=updated["channel"],
        thread_ts=updated["thread_ts"],
        text=f"{project_name} updated",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"✏️ <@{user_id}> updated *{project_name}*\n"
                    + "\n".join(f"• {c}" for c in shown)
                    + more,
                },
            }
        ],
    )

    # Changing type changes which gates apply, so say so rather than leaving a
    # checklist that no longer belongs to this project sitting in the thread.
    if project_type != was_type:
        if was_type in gates.GATED_TYPES and project_type not in gates.GATED_TYPES:
            note = (
                f"This is now a *{type_label}*, which has no checklist. The Steps 2–6 "
                f"message above no longer applies — its ticks are kept for the record."
            )
        elif project_type in gates.GATED_TYPES and was_type not in gates.GATED_TYPES:
            note = (
                f"This is now a *{type_label}*, which does run the checklist. Close it "
                f"and raise it again to start one."
            )
        else:
            note = f"Project type is now *{type_label}*."
        client.chat_postMessage(
            channel=updated["channel"], thread_ts=updated["thread_ts"], text=note,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": f"⚠️ {note}"}}],
        )

    before = run.get("record") or {}
    touched = [k for k in ingestion.ELEMENT_KEYS if before.get(k) != record.get(k)]
    cleared = ingestion.invalidate(run_id, touched) if touched else []
    if touched:
        ingestion.sync_auto(run_id, record)
    # A blank optional element going automatic is not something anyone needs to redo.
    cleared = [k for k in cleared if not _blank(record.get(k))]
    if cleared:
        labels = [e["label"] for e in ingestion.ELEMENTS if e["key"] in cleared]
        _say(client, updated, "↩️ These changed after Delivery signed them off, so they need "
                              "signing again: " + ", ".join(labels))
        _refresh_ingestion(client, run_id)

    _write_sheet_later(sheets.update_row, run_id, record)
    _push_doc(run_id)
    if project_type in gates.GATED_TYPES:
        _refresh(client, run_id)


@app.action(re.compile(rf"^{OWNERS_ACTION}:"))
def on_change_owners(ack, body, action, client):
    ack()
    run = _run_or_tell(client, body, action["action_id"].split(":")[1])
    if run:
        client.views_open(trigger_id=body["trigger_id"], view=owners_view(run))


@app.view(OWNERS_CALLBACK)
def on_owners_submitted(ack, body, view, client):
    ack()
    run_id = view["private_metadata"]
    values = view["state"]["values"]
    owners = {
        key: values[key][key]["selected_user"]
        for key in ("research_owner", "delivery_owner", "sales_owner")
        if values.get(key, {}).get(key, {}).get("selected_user")
    }
    try:
        _, changes, warning = gates.set_owners(run_id, owners, body["user"]["id"])
    except gates.RunGone:
        _tell(client, body, GONE_MESSAGE)
        return

    run = gates.get_run(run_id)
    if changes:
        text = (
            f"👥 <@{body['user']['id']}> changed owners on *{run['project_name']}*\n"
            + "\n".join(f"• {c}" for c in changes)
        )
        if warning:
            text += f"\n⚠️ {warning}"
        client.chat_postMessage(
            channel=run["channel"],
            thread_ts=run["thread_ts"],
            text="Owners changed",
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
        )
    _refresh(client, run_id)
    _refresh_ingestion(client, run_id)


@app.action(re.compile(rf"^{CLOSE_ACTION}:"))
def on_close_clicked(ack, body, action, client):
    ack()
    run = _run_or_tell(client, body, action["action_id"].split(":")[1])
    if run:
        client.views_open(trigger_id=body["trigger_id"], view=close_view(run))


@app.view(CLOSE_CALLBACK)
def on_close_submitted(ack, body, view, client):
    ack()
    run_id = view["private_metadata"]
    values = view["state"]["values"]
    reason = values["reason"]["reason"]["selected_option"]["value"]
    note = values.get("note", {}).get("note", {}).get("value") or ""

    try:
        gates.close_run(run_id, body["user"]["id"], reason, note)
    except gates.RunGone:
        _tell(client, body, GONE_MESSAGE)
        return
    run = gates.get_run(run_id)
    client.chat_postMessage(
        channel=run["channel"],
        thread_ts=run["thread_ts"],
        text=f"{run['project_name']} closed",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"🔒 <@{body['user']['id']}> closed *{run['project_name']}* — "
                    f"{gates.CLOSE_REASON_LABELS.get(reason, reason)}"
                    + (f"\n_{note}_" if note else ""),
                },
            }
        ],
    )
    _refresh(client, run_id)


@app.action(re.compile(rf"^{REOPEN_ACTION}:"))
def on_reopen(ack, body, action, client):
    ack()
    run_id = action["action_id"].split(":")[1]
    if not _run_or_tell(client, body, run_id):
        return
    gates.reopen_run(run_id, body["user"]["id"])
    run = gates.get_run(run_id)
    client.chat_postMessage(
        channel=run["channel"],
        thread_ts=run["thread_ts"],
        text=f"{run['project_name']} reopened",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"🔓 <@{body['user']['id']}> reopened *{run['project_name']}*. "
                    "Every tick and sign-off is as it was.",
                },
            }
        ],
    )
    _refresh(client, run_id)


@app.action(re.compile(rf"^{RETURN_ACTION}:"))
def on_gate_return(ack, body, action, client):
    ack()
    _, run_id = action["action_id"].split(":")
    if not _run_or_tell(client, body, run_id):
        return
    gates.return_to_taxonomy(run_id, body["user"]["id"])
    run = gates.get_run(run_id)
    client.chat_postMessage(
        channel=run["channel"],
        thread_ts=run["thread_ts"],
        text=(
            f"↩️ <@{body['user']['id']}> returned *{run['project_name']}* to the taxonomy. "
            "Seed set and alignment gate cleared — rework the taxonomy, do not patch the samples."
        ),
    )
    _refresh(client, run_id)


@app.error
def on_unhandled(error, body, client, logger):
    """Last line of defence: never leave a click with no response at all.

    Handlers ack() early, so an exception after that point is invisible to the
    user — the button simply does nothing. Telling them something went wrong is
    worth more than the silence, even when there is nothing useful to say.
    """
    logger.exception("Unhandled listener error: %s", error)
    user = ((body or {}).get("user") or {}).get("id")
    channel = ((body or {}).get("channel") or {}).get("id")
    if not user or not channel:
        return
    try:
        client.chat_postEphemeral(
            channel=channel,
            user=user,
            text="That didn't go through — something broke on the bot's side. "
            "Try again, and tell whoever runs it if it keeps happening.",
        )
    except Exception:
        logger.exception("Could not report the failure to the user either")


def _claim_single_instance() -> None:
    """Refuse to start if another copy is already connected.

    Socket Mode lets several processes of the same app hold connections at once,
    and Slack delivers to whichever is free — so a forgotten instance answers
    some clicks with stale code, and the symptoms look like random flakiness
    rather than a second process. Better to fail loudly at startup.
    """
    import atexit

    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.pid")
    if os.path.exists(lock_path):
        try:
            with open(lock_path) as f:
                existing = int(f.read().strip())
            os.kill(existing, 0)  # signal 0 only tests whether the pid is alive
        except (ValueError, OSError):
            pass  # stale file from a process that is gone
        else:
            raise SystemExit(
                f"Another intake bot is already running (pid {existing}).\n"
                f"Stop it first:  kill {existing}\n"
                f"If you are sure it is gone, delete {lock_path}."
            )

    with open(lock_path, "w") as f:
        f.write(str(os.getpid()))

    def release():
        try:
            with open(lock_path) as f:
                if int(f.read().strip()) == os.getpid():
                    os.remove(lock_path)
        except (ValueError, OSError):
            pass

    atexit.register(release)


def _report_downtime() -> None:
    """Say how long the bot was unreachable, once it can speak again.

    A stopped process cannot raise an alarm, so the outage is reported on the way
    back up. Without this, downtime is discovered by whoever next types
    `/new-project` and gets "the app did not respond" — which is how the last
    one was found.
    """
    gap = watchdog.downtime_seconds()
    if gap is None or gap < watchdog.OUTAGE_THRESHOLD_SECONDS:
        return

    spell = watchdog.describe(gap)
    since = dt.datetime.now() - dt.timedelta(seconds=gap)
    logger.warning("Bot was unreachable for %s (since %s)", spell, since.strftime("%H:%M"))

    channel = os.environ.get("ALERT_CHANNEL", "").strip()
    if not channel:
        return
    try:
        app.client.chat_postMessage(
            channel=channel,
            text=f"Intake bot was unreachable for {spell}",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"⚠️ *Intake bot was unreachable for {spell}* — "
                        f"back up now.\nAnything anyone tried since "
                        f"{since.strftime('%H:%M')} did not go through and needs "
                        f"raising again.",
                    },
                }
            ],
        )
    except Exception:
        logger.exception("Could not post the downtime notice to %s", channel)


def _configure_log_rotation() -> None:
    """Cap the log on disk.

    A wedged socket writes several lines per reconnect attempt; one bad night
    produced ~45,000 lines. Left alone it grows without limit and makes the very
    diagnosis it exists for slower.
    """
    from logging.handlers import RotatingFileHandler

    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)

    handler = RotatingFileHandler(
        os.path.join(log_dir, "intake.log"), maxBytes=2_000_000, backupCount=3
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.addHandler(handler)


if __name__ == "__main__":
    _claim_single_instance()
    _configure_log_rotation()
    gates.init_db()
    if not sheets.enabled():
        logger.warning("Google Sheets not configured — submissions post to the channel only")

    # Measured before the heartbeat starts writing, or it would overwrite the
    # very timestamp the gap is calculated from.
    _report_downtime()
    watchdog.start_heartbeat()
    _ingestion_sweep(app.client)

    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])

    # Running is not the same as connected: the socket can die and reconnect in
    # a loop forever without the process ever exiting, so nothing downstream
    # notices. This exits when that happens, and launchd starts a clean one.
    watchdog.start(
        is_connected=lambda: handler.client is not None and handler.client.is_connected(),
        on_give_up=lambda: _sheet_writer.shutdown(wait=False),
    )

    handler.start()
