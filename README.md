# Project Intake — dynamic Slack form

A Slack modal whose fields change as you fill it in. Picking a project type loads
that type's entire form; changing a controlling field (priority, "does this touch
PII?", tooling choice) shows or hides dependent fields immediately.

Slack's built-in Workflow Builder forms are static — no branching, no conditional
fields — so this is a small Bolt app instead. It runs in Socket Mode, so it needs
no public URL and no ngrok.

The **Sample** form mirrors Steps 0 and 1 of the Sample Creation Checklist
(Step 0 "should we do it?" gate, then the parallel Research / Delivery /
Engineering questions). Steps 2–6 play out over days, so they run as a tracked
thread under the intake message rather than as form fields — see
[Gate tracking](#gate-tracking).

## Files

| File | Purpose |
| --- | --- |
| `form_schema.json` | The entire form definition. Edit this, not the Python. |
| `form_builder.py` | Schema → Block Kit modal; modal state → plain dict. |
| `app.py` | Slack handlers: open, re-render, submit, gate interactions. |
| `gates.py` | Steps 2–6: definitions, rules, SQLite persistence. |
| `gate_blocks.py` | The checklist message. |
| `sheets.py` | Appends each submission to a Google Sheet. |
| `check_sheets.py` | Preflight for the Google Sheets connection. |
| `test_form.py` | Offline checks on the view payloads — no Slack needed. |
| `test_gates.py` | Drives a full run through all six gates. |
| `test_sheets.py` | Column management against a fake worksheet. |
| `preview.py` | Renders every state as an HTML review sheet. |
| `manifest.yaml` | Slack app manifest (scopes, command, Socket Mode). |

## Setup

1. Go to <https://api.slack.com/apps> → **Create New App** → **From a manifest**,
   pick your workspace, and paste the contents of `manifest.yaml`.
2. **Basic Information** → **App-Level Tokens** → **Generate Token and Scopes**.
   Name it anything, add the `connections:write` scope, and copy the `xapp-` token.
3. **Install App** → install to workspace, then copy the Bot User OAuth Token
   (`xoxb-`).
4. Copy `.env.example` to `.env` and fill in both tokens.
5. Invite the bot to the intake channel: `/invite @Project Intake`.

Then install and run:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
set -a && source .env && set +a && .venv/bin/python app.py
```

Type `/new-project` in the channel.

### Google Sheets (optional)

Without credentials the bot posts to the channel only and logs a warning at
startup — everything else works.

A service account is a robot Google account with its own email address. You
share the sheet with it exactly as you would with a colleague; there is no OAuth
consent screen and nothing to re-authorise later.

1. Open the [Google Cloud console](https://console.cloud.google.com/) and create
   or pick a project.
2. Under **APIs & Services → Library**, enable both the **Google Sheets API**
   and the **Google Drive API**.
3. Under **APIs & Services → Credentials**, create a **service account**. Then
   open it, go to the **Keys** tab, and choose **Add key → Create new key →
   JSON**. The file downloads once and cannot be re-downloaded — save it into
   this folder as `service-account.json`.
4. Open the JSON and copy the `client_email` value. It looks like
   `something@your-project.iam.gserviceaccount.com`.
5. Open your spreadsheet, press **Share**, paste that address, and give it
   **Editor** access. Skipping this is the single most common failure — the
   symptom is `SpreadsheetNotFound`, which reads like a wrong ID but is really a
   permission problem.
6. Copy the sheet id out of its URL — the long string between `/d/` and `/edit` —
   and set `GOOGLE_SHEET_ID` and `GOOGLE_SERVICE_ACCOUNT_JSON` in `.env`.

Then prove the connection before running the bot:

```bash
set -a && source .env && set +a && .venv/bin/python check_sheets.py --write
```

It reports the service-account address, the spreadsheet title, the tabs it can
see, and the current header. With `--write` it appends one row labelled
`DELETE ME — connection test` and tells you to delete it — the script never
deletes anything itself. Each failure it reports names the specific fix.

The `.gitignore` already excludes `service-account.json`. That key grants write
access to every sheet it has been shared with, so keep it out of version control
and out of Slack.

Columns are managed automatically — a new field in the schema becomes a new
column on the next submission. Nothing in the sheet needs editing by hand. Each
gated run also gets a `run_id` and a `gate_status` column, and the status is
rewritten on every checklist interaction so the sheet stays true for people who
are not in the thread.

## Editing the form

Everything is in `form_schema.json`. Add a project type by adding a key under
`project_types`; add a field by adding an object to a `fields` list.

```json
{
  "key": "vendor_name",
  "label": "Which vendor?",
  "type": "text",
  "optional": false,
  "hint": "Shown under the input",
  "placeholder": "Shown inside the input",
  "show_if": { "field": "tooling", "in": ["vendor"] }
}
```

Field types: `text`, `textarea`, `number`, `url`, `email`, `select`, `multi_select`,
`radio`, `checkboxes`, `date`, `user`, `multi_user`, plus `context` and `divider`
for section headers.

`select`, `multi_select`, `radio` and `checkboxes` need an `options` list of
`{"label": ..., "value": ...}`. `number` takes optional `min_value` / `max_value`.

`context` fields render as a bold label with no input. They still need a unique
`key`, and they accept `show_if` like anything else, so a whole section can be
hidden at once. They never reach the sheet and are never required.

Keys must be unique across `common_fields` plus any one project type — two
fields sharing a key would collide as a Slack `block_id`. `test_form.py` checks
this, along with every `show_if` pointing at a field that exists.

`show_if` takes `{"field": <another field's key>, "in": [<values>]}`. Chains work:
if A reveals B and B reveals C, hiding A hides both.

`required_if` has the same shape and turns an optional field mandatory when the
condition holds. Most of the form describes work that may not have started, so
it is optional by default — but the moment someone answers *Research underway*,
that is a claim the research answers exist, and the form asks for them:

| Stage | Required |
| --- | --- |
| Just scoping | identity and the Step 0 go/no-go only |
| Research underway | + failure pattern, taxonomy, benchmark, endpoints probed |
| Ready to build | + annotator profile, guideline v0, lifecycle, endpoint access, harness, seed count, synthetic |

Any field named by a `show_if` or a `required_if` automatically becomes a
re-render trigger — there is no flag to remember to set.

After editing, check the schema before restarting:

```bash
.venv/bin/python test_form.py
```

## Opportunity ingestion

From Delivery's *Opportunity Ingestion Checklist*: every new opportunity —
internal or customer-driven — gives Delivery six things before work starts, and
Delivery owes a go/no-go within **36 hours** of the request.

The form opens with four questions for every type: whether this is a new
opportunity or a recurring account, the request signal, the requesting team and
the requestor. New opportunities then answer the six elements — specifications
document, volume, example task or customer spec, other crucial details, TAT and
progress milestones. **Recurring accounts** (GHealth, Bluedog, BotCo…) skip them:
they are ingested once, not on every scope change.

Customer and Sales owner are required only for customer pilots and scope
extensions — internal ML/FDE and research requests have neither.

Submitting posts the ingestion checklist in the thread and pings the Delivery
owner. From there:

| The doc says | What the bot does |
| --- | --- |
| Each element signed off, with a date | Only the Delivery owner can tick them; each tick is dated, and re-ticking keeps the original date |
| Go/no-go within 36 hours | A countdown on the message; a reminder at 30 hours and another when it passes |
| Clarifications come back to the requestor | *Ask the requestor* posts the question in the thread, tagged |
| Final sign-off: requestor + Delivery owner | Go unlocks both sign-off buttons; each accepts only the named person |

Go needs every element signed off. A no-go needs a reason the requestor can act
on, and closes the project — reopenable, nothing deleted. Editing an element
after Delivery signed it clears that sign-off, since they signed off on what they
read; once go is called the ingestion is settled and edits leave it alone.

For Samples, the Steps 2–6 checklist is posted only once ingestion is fully
signed off. Ship stays blocked until then as well.

## Gate tracking

Submitting a **Sample** posts the summary to the channel, then opens the Steps
2–6 checklist in a thread beneath it. It is one message, rewritten in place with
`chat_update` as the run advances. Steps live in `gates.py`; only project types
listed in `GATED_TYPES` get one.

Steps are **not** forced into single file — real work overlaps, and blocking
that only teaches people to tick boxes early. The audit request gets drafted
while expansion runs; reports get written while the audit is open.

The checklist forbids exactly two things outright, and those are the only hard
gates:

| Checklist says | What the bot does |
| --- | --- |
| "Never expand past an unvetted seed set" | Step 4 renders with no controls until the alignment gate passes. Every other step is open from the start. |
| "Never the same day" | Ticking "request posted" starts a clock. Ship stays blocked for 24h, and un-ticking does not wind it back. |

Three more rules are enforced because they are cheap to enforce and expensive to
get wrong:

| Checklist says | What the bot does |
| --- | --- |
| "Do not patch the samples and continue" | The no-go button clears Steps 2 and 3 and their sign-offs, and logs who returned it. |
| "Research and Delivery sign off jointly" | Sign-off buttons reject anyone who is not the current named owner, and reject any step with unticked items. |
| "If any answer is no, it does not ship" | The ship banner lists every outstanding item, missing sign-off, and remaining audit time. |

Editing a step that was already signed off clears its sign-offs — people signed
off on what they read, not on whatever it becomes afterwards.

### Correcting an intake

**Edit**, on the channel summary, reopens the intake form filled in with what
was submitted. Saving rewrites the record, the summary message and the sheet
row, and posts a field-by-field diff in the thread — *Customer: Tencnt →
Tencent* — so a correction leaves a trail rather than quietly rewriting history.

The checklist is deliberately untouched by an edit. Fixing a date or a customer
name says nothing about whether the seed set was any good, so ticks and sign-offs
stay exactly as they were.

Changing the **project type** in an edit is allowed, and says so in the thread:
moving off a gated type leaves the existing checklist in place but no longer
applicable, and moving onto one does not start a checklist retrospectively —
close it and raise it again if that is what you want.

### When people or plans change

**Change owners** hands a role to someone else. Sign-offs already given stay
recorded (a handover does not un-review the work), but outstanding ones move to
the new owner and the previous one can no longer sign. The change is posted in
thread.

**Close project** stops the checklist with a reason — delivered, no go, customer
dropped, superseded, or stalled — plus an optional note. Nothing is deleted: the
run renders read-only with every tick still visible, and **Reopen** restores it
exactly as it was.

State lives in SQLite at `gates.db` (override with `GATES_DB`). Nothing is
stored in the message itself, so a run survives restarts and edits.

## Running it

The bot is a long-running process that holds an outbound WebSocket to Slack.
There is no server and no public URL — but there is also nothing running it but
this process. **While it is down, `/new-project` simply fails for everyone.**
Nothing queues; nothing arrives later.

A launchd agent keeps it up across logins and crashes:

```bash
./install-service.sh install
```

```bash
./install-service.sh status
```

`restart` reloads it after a code change, `stop` pauses it, `uninstall` removes
it. Logs go to `logs/bot.log`.

To run it in a terminal instead — useful when you want to watch it work:

```bash
./run.sh
```

The bot writes `app.pid` and refuses to start if another copy is already
connected. This matters more than it looks: Socket Mode lets several processes
of the same app hold connections at once, and Slack delivers each event to
whichever is free — so a forgotten instance answers some clicks with stale code,
and the symptom is intermittent flakiness rather than an obvious duplicate.

### The failure launchd cannot see

`KeepAlive` restarts a process that **exits**. Socket Mode has a worse failure
than exiting: the websocket dies — a laptop sleeping is enough — and the client
reconnects forever, hitting the same broken pipe every time. The process stays
alive, `launchctl` reports it healthy, and Slack answers `/new-project` with
*"the app did not respond"*. This happened, and ran for 4,937 reconnect attempts
before anyone noticed.

`watchdog.py` watches the one thing that matters — whether the socket is
actually connected — and exits the process once it has been down about two
minutes, so launchd starts a clean one. A brief disconnect is ignored; ordinary
reconnects take seconds.

Logs rotate at 2 MB, four files, in `logs/intake.log`. The wedged socket wrote
4.6 MB of reconnect noise in a night, which made diagnosing it slower than it
should have been.

### What the laptop still cannot do

launchd restarts the bot after a crash, a logout or a reboot, and the watchdog
covers a wedged connection. Neither helps while the Mac is **asleep, shut down,
or off the network** — the bot is simply gone for that whole period, and so is
every intake and checklist click anyone tries in the meantime.

That is fine while the team is small and you are the one using it. Before other
people depend on it, move it to something always-on: any host with outbound
internet works, since Socket Mode needs no inbound ports. Three things travel
with it — `gates.db` needs a persistent volume or it takes every checklist with
it on redeploy, the service-account key needs to come from a secret rather than
a file path, and `.env` becomes the host's environment.

### Why it does not live in ~/Downloads

macOS blocks launchd agents from executing anything inside Downloads, Desktop
or Documents, so auto-start does not work from there. The folder also holds
Slack tokens and a Google private key, which is not what Downloads is for.

Drive a full run offline, including the 24-hour rule:

```bash
.venv/bin/python test_gates.py
```

## Reviewing changes before they reach Slack

`preview.py` renders every modal state *and* the gate thread to a single HTML
page, drawn from the same payloads the bot sends. Edit the schema, regenerate,
and read the result before anyone installs anything:

```bash
.venv/bin/python preview.py
```

## How the re-render works

Slack has no client-side conditional logic. Trigger fields carry
`dispatch_action: true`, so changing one sends a `block_actions` event; the app
rebuilds the whole view from the schema and calls `views.update`.

The gotcha this handles: `views.update` replaces every block, so already-typed
answers vanish unless you put them back. `extract_state()` reads
`view.state.values` and `build_view()` re-injects each value as `initial_value` /
`initial_option` / `initial_date` / `initial_user`. Switching project type
deliberately keeps the common header fields and drops the old type's answers.

Hidden fields aren't rendered, so Slack never reports them in state and never
enforces `optional: false` on them. Required-field validation for the visible set
runs in `on_submit` and returns `response_action: "errors"`, which shows the
error inline instead of closing the modal.

## Limits worth knowing

- A modal holds at most 100 blocks. `build_view` raises if a type exceeds 90
  visible fields; `test_form.py` checks each type.
- `views.update` is sent with the view `hash` to guard against races. On
  `hash_conflict` (fast double-click) the app retries without the hash.
- Only one modal per `trigger_id`, and trigger ids expire after 3 seconds — the
  handlers `ack()` first, before any other work.
