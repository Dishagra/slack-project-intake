"""Answers must survive a field being hidden and shown again.

Slack reports view state only for blocks it is currently rendering, so hiding a
field erases what was typed into it unless something outside the view remembers.
This models Slack's behaviour faithfully — only rendered blocks report values —
and checks the accumulator that fills the gap.

Run: python test_answers.py
"""

import os

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test")

import functools

import slack_bolt

# Constructing App() calls auth.test; this test never talks to Slack.
slack_bolt.App.__init__ = functools.partialmethod(
    slack_bolt.App.__init__, token_verification_enabled=False, request_verification_enabled=False
)

import app  # noqa: E402
from form_builder import TYPE_KEY, build_view, extract_state, load_schema  # noqa: E402

SCHEMA = load_schema()
failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail and not cond else ''}")
    if not cond:
        failures.append(name)


def slack_view(view, answers, view_id="V1"):
    """What Slack would send back: state for rendered blocks only."""
    values = {}
    for block in view["blocks"]:
        if block.get("type") != "input":
            continue
        key = block["block_id"]
        if key not in answers or answers[key] in (None, "", []):
            continue
        value = answers[key]
        payload = (
            {"type": "multi_static_select", "selected_options": [{"value": v} for v in value]}
            if isinstance(value, list)
            else {"type": "plain_text_input", "value": value}
        )
        values[key] = {key: payload}
    return {"id": view_id, "hash": "h", "private_metadata": "", "blocks": view["blocks"],
            "state": {"values": values}}


def initial_values(view):
    return {
        b["block_id"]: b["element"].get("initial_value")
        or b["element"].get("initial_option", {}).get("value")
        for b in view["blocks"]
        if b.get("type") == "input"
    }


VIEW_ID = "V_TEST"
app._forget(VIEW_ID)

FILLED = {
    TYPE_KEY: "sample",
    "intake_stage": "ready",
    "project_name": "Multi-Turn Attack Bench",
    "account": "Tencent",
    "failure_pattern": "context drift across turns",
    "taxonomy_status": "v0_ready",
    "seed_count": "3",
    "sample_size": "500",
    "harness_status": "ready",
}

# 1. the user fills the long form
view = build_view(SCHEMA, "sample", FILLED)
live = extract_state(slack_view(view, FILLED, VIEW_ID))
accumulated = app._remember(VIEW_ID, live)
check("everything typed is remembered", len(accumulated) >= 9, str(len(accumulated)))

# 2. they flip the stage back to scoping, which hides the whole Step 1 block
collapsed = dict(FILLED)
collapsed["intake_stage"] = "scoping"
view2 = build_view(SCHEMA, "sample", collapsed)
reported = extract_state(slack_view(view2, collapsed, VIEW_ID))
check("Slack reports far less after hiding", len(reported) < len(accumulated),
      f"{len(reported)} vs {len(accumulated)}")
check("failure_pattern is not in Slack's report", "failure_pattern" not in reported)

merged = app._remember(VIEW_ID, reported)
check("accumulator keeps the hidden answers", merged.get("failure_pattern") == "context drift across turns")
check("accumulator honours the new stage", merged["intake_stage"] == "scoping")

# 3. they flip back — the answers must reappear in the rendered form
restored = dict(merged)
restored["intake_stage"] = "ready"
view3 = build_view(SCHEMA, "sample", app._remember(VIEW_ID, {"intake_stage": "ready"}))
values = initial_values(view3)
check("failure_pattern comes back", values.get("failure_pattern") == "context drift across turns")
check("taxonomy_status comes back", values.get("taxonomy_status") == "v0_ready")
check("seed_count comes back", values.get("seed_count") == "3")
check("sample_size comes back", values.get("sample_size") == "500")
for key in ("project_name", "account", "harness_status"):
    check(f"{key} survived the round trip", values.get(key) not in (None, ""), str(values.get(key)))

# 4. a later edit wins over the remembered value
app._remember(VIEW_ID, {"failure_pattern": "rewritten"})
view4 = build_view(SCHEMA, "sample", app._answers[VIEW_ID])
check("newer answer overwrites the remembered one",
      initial_values(view4).get("failure_pattern") == "rewritten")

# 5. submitting clears the modal's entry
app._forget(VIEW_ID)
check("forgetting drops the entry", VIEW_ID not in app._answers)

# 6. the accumulator cannot grow without bound
for i in range(app._MAX_TRACKED_VIEWS + 25):
    app._remember(f"V{i}", {"project_name": str(i)})
check("tracked views stay capped", len(app._answers) <= app._MAX_TRACKED_VIEWS,
      str(len(app._answers)))

# 7. switching project type keeps what the destination also asks
pilot_answers = {
    TYPE_KEY: "pilot", "project_name": "Acme pilot", "account": "Acme",
    "uses_synthetic": "yes", "tooling": "vendor", "contains_pii": "yes",
    "expertise": ["swe"], "pilot_value": "50000",
}
app._forget("V_SWITCH")
app._remember("V_SWITCH", pilot_answers)

from form_builder import all_fields  # noqa: E402

dest = {f["key"] for f in all_fields(SCHEMA, "production")}
wanted = app.COMMON_KEYS | dest
carried = {k: v for k, v in app._answers["V_SWITCH"].items() if k in wanted or k == TYPE_KEY}
shared = {"uses_synthetic", "tooling", "contains_pii", "expertise"}
check("shared questions carry across a type switch", shared <= set(carried), str(sorted(carried)))
check("pilot-only answers are dropped", "pilot_value" not in carried)
check("common answers always carry", {"project_name", "account"} <= set(carried))

# 8. a re-render that changes nothing must not be sent
#    Every views.update rebuilds the modal in the client and drops keyboard
#    focus, so a pointless one yanks the cursor out of whatever is being typed.
from form_builder import trigger_keys  # noqa: E402

sent, skipped = [], []


class FakeClient:
    def views_update(self, **kwargs):
        sent.append(kwargs)


def rerender(project_type, before_state, after_state):
    """Model one trigger change: current view, new answers, did we push?"""
    before = build_view(SCHEMA, project_type, before_state)
    body = {"view": {"id": "V", "hash": "h", "private_metadata": "", "blocks": before["blocks"]}}
    count = len(sent)
    app._rerender(FakeClient(), body, project_type, after_state)
    return len(sent) > count


base = {TYPE_KEY: "sample", "intake_stage": "underway"}

# Urgency high -> medium -> low all hide the justification: same form each time.
check("no push when the form is unchanged",
      not rerender("sample", {**base, "urgency": "high"}, {**base, "urgency": "medium"}))
check("no push between two other equivalent options",
      not rerender("sample", {**base, "urgency": "medium"}, {**base, "urgency": "low"}))

# Critical reveals a field, so that one genuinely has to go.
check("pushes when a field appears",
      rerender("sample", {**base, "urgency": "high"}, {**base, "urgency": "critical"}))
check("pushes when a field disappears",
      rerender("sample", {**base, "urgency": "critical"}, {**base, "urgency": "high"}))
check("pushes when a whole section opens",
      rerender("sample", {**base, "intake_stage": "scoping"},
               {**base, "intake_stage": "underway"}))

# Across the whole schema, how much noise did this remove?
pointless = real = 0
for name in SCHEMA["project_types"]:
    fields = {f["key"]: f for f in app.all_fields(SCHEMA, name)}
    for key in trigger_keys(SCHEMA):
        field = fields.get(key)
        if not field or not field.get("options") or len(field["options"]) < 2:
            continue
        start = {TYPE_KEY: name, "intake_stage": "underway"}
        for option in field["options"][1:]:
            prev = {**start, key: field["options"][0]["value"]}
            nxt = {**start, key: option["value"]}
            if app._layout(build_view(SCHEMA, name, prev)["blocks"]) == app._layout(
                build_view(SCHEMA, name, nxt)["blocks"]
            ):
                pointless += 1
            else:
                real += 1

check("a meaningful share of re-renders is now skipped", pointless > 0)
check("the ones that matter still happen", real > pointless)
print(f"      {pointless} of {pointless + real} trigger changes no longer rebuild the modal")

print()
print(f"{len(failures)} failure(s)" if failures else "all checks passed")
raise SystemExit(1 if failures else 0)
