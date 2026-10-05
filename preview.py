"""Render the real Block Kit payloads as an HTML review sheet.

This does not mock the form up by hand — it calls build_view() and draws
whatever comes back, so the preview and Slack are fed from the same source.
Conditional blocks are flagged with the field that revealed them.

Run: python preview.py   ->  writes preview.html
"""

from __future__ import annotations

import html
import os
from typing import Any, Dict, List

from form_builder import TYPE_KEY, build_view, load_schema, visible_fields

SCHEMA = load_schema()
OUT = os.path.join(os.path.dirname(__file__), "preview.html")


# --------------------------------------------------------------------------
# which blocks are conditional, and on what
# --------------------------------------------------------------------------

def condition_map(project_type: str) -> Dict[str, str]:
    """field key -> human phrase naming the answer that revealed it."""
    labels: Dict[str, str] = {}
    options: Dict[str, Dict[str, str]] = {}
    for f in SCHEMA["common_fields"] + SCHEMA["project_types"][project_type]["fields"]:
        labels[f["key"]] = f["label"]
        options[f["key"]] = {o["value"]: o["label"] for o in f.get("options", [])}

    out: Dict[str, str] = {}
    for f in SCHEMA["common_fields"] + SCHEMA["project_types"][project_type]["fields"]:
        cond = f.get("show_if")
        if not cond:
            continue
        parent = cond["field"]
        shown = " / ".join(options.get(parent, {}).get(v, v) for v in cond["in"])
        out[f["key"]] = f"{labels.get(parent, parent)} = {shown}"
    return out


# --------------------------------------------------------------------------
# block -> html
# --------------------------------------------------------------------------

def e(s: Any) -> str:
    return html.escape(str(s))


def _opt_labels(el: Dict[str, Any]) -> List[str]:
    return [o["text"]["text"] for o in el.get("options", [])]


def _selected(el: Dict[str, Any]) -> List[str]:
    if "initial_option" in el:
        return [el["initial_option"]["text"]["text"]]
    return [o["text"]["text"] for o in el.get("initial_options", [])]


def render_element(el: Dict[str, Any]) -> str:
    t = el["type"]

    if t in ("plain_text_input", "url_text_input", "email_text_input", "number_input"):
        value = el.get("initial_value", "")
        placeholder = el.get("placeholder", {}).get("text", "")
        tall = ' style="min-height:66px"' if el.get("multiline") else ""
        inner = (
            f'<span class="val">{e(value)}</span>'
            if value
            else f'<span class="ph">{e(placeholder)}</span>'
        )
        bounds = ""
        if el.get("min_value") or el.get("max_value"):
            lo, hi = el.get("min_value", "—"), el.get("max_value", "—")
            bounds = f'<span class="bound">{e(lo)}–{e(hi)}</span>'
        return f'<div class="field"{tall}>{inner}{bounds}</div>'

    if t in ("static_select", "multi_static_select", "users_select", "multi_users_select"):
        chosen = _selected(el)
        if t == "users_select" and el.get("initial_user"):
            chosen = ["@" + el["initial_user"]]
        if t == "multi_users_select" and el.get("initial_users"):
            chosen = ["@" + u for u in el["initial_users"]]
        placeholder = el.get("placeholder", {}).get("text", "Select")
        if chosen:
            inner = "".join(f'<span class="chip">{e(c)}</span>' for c in chosen)
        else:
            inner = f'<span class="ph">{e(placeholder)}</span>'
        return f'<div class="field select">{inner}<span class="caret">⌄</span></div>'

    if t == "datepicker":
        value = el.get("initial_date")
        inner = (
            f'<span class="val">{e(value)}</span>'
            if value
            else f'<span class="ph">{e(el.get("placeholder", {}).get("text", "Pick a date"))}</span>'
        )
        return f'<div class="field select"><span class="ico">▤</span>{inner}</div>'

    if t in ("radio_buttons", "checkboxes"):
        chosen = set(_selected(el))
        shape = "radio" if t == "radio_buttons" else "check"
        items = "".join(
            f'<span class="opt"><i class="{shape}{" on" if lab in chosen else ""}"></i>{e(lab)}</span>'
            for lab in _opt_labels(el)
        )
        return f'<div class="opts">{items}</div>'

    return f'<div class="field"><span class="ph">{e(t)}</span></div>'


def render_block(block: Dict[str, Any], conds: Dict[str, str]) -> str:
    if block["type"] == "divider":
        return '<hr class="rule">'

    if block["type"] == "context":
        raw = block["elements"][0]["text"]
        key = block.get("block_id", "")
        # Slack renders _..._ as italic prose; section headers use *...* instead.
        if raw.startswith("_") and raw.endswith("_"):
            return f'<div class="aside">{e(raw.strip("_"))}</div>'
        why = conds.get(key)
        tag = f'<span class="why">{e(why)}</span>' if why else ""
        return f'<div class="sect">{e(raw.replace("*", ""))}{tag}</div>'

    key = block.get("block_id", "")
    label = block["label"]["text"]
    hint = block.get("hint", {}).get("text")
    why = conds.get(key)

    meta = []
    if block.get("optional"):
        meta.append('<span class="tag opt-tag">optional</span>')
    if block.get("dispatch_action"):
        meta.append('<span class="tag trig">re-renders form</span>')

    parts = [f'<div class="row{" cond" if why else ""}">']
    parts.append('<div class="lab">')
    parts.append(f"<span>{e(label)}</span>")
    parts.append(f'<code>{e(key)}</code>')
    parts.extend(meta)
    parts.append("</div>")
    if why:
        parts.append(f'<div class="why-line">shown because <b>{e(why)}</b></div>')
    parts.append(render_element(block["element"]))
    if hint:
        parts.append(f'<div class="hint">{e(hint)}</div>')
    parts.append("</div>")
    return "".join(parts)


def render_modal(title: str, view: Dict[str, Any], conds: Dict[str, str]) -> str:
    body = "".join(render_block(b, conds) for b in view["blocks"])
    submit = view.get("submit", {}).get("text")
    buttons = '<button class="btn ghost">Cancel</button>'
    if submit:
        buttons += f'<button class="btn go">{e(submit)}</button>'
    else:
        buttons += '<button class="btn go dis" disabled>No submit yet</button>'
    return (
        '<div class="modal">'
        f'<div class="bar"><span class="t">{e(title)}</span><span class="x">✕</span></div>'
        f'<div class="body">{body}</div>'
        f'<div class="foot">{buttons}</div>'
        "</div>"
    )


# --------------------------------------------------------------------------
# the states worth reviewing
# --------------------------------------------------------------------------

STATES = [
    {
        "name": "Fresh open",
        "note": "Nothing but the type picker. No submit button — there is nothing to submit yet, "
        "and offering one would only produce an error.",
        "type": None,
        "state": {},
    },
    {
        "name": "Sample, untouched",
        "note": "The moment a type is picked, the whole form arrives. Every conditional field is "
        "absent — this is the shortest the Sample form ever is.",
        "type": "sample",
        "state": {TYPE_KEY: "sample"},
    },
    {
        "name": "Sample, reuse path",
        "note": "Critical urgency opens a justification. 'Existing asset: yes' asks which one — "
        "this is the ARGO case from the Tencent checklist.",
        "type": "sample",
        "state": {
            TYPE_KEY: "sample",
            "project_name": "Multi-Turn Attack Bench",
            "account": "Tencent",
            "workstream": "red_team",
            "research_owner": "Tanmay Asthana",
            "delivery_owner": "Shravan Pendem",
            "sales_owner": "Fawaz Sharief",
            "urgency": "critical",
            "urgency_justification": "Customer eval window closes end of quarter.",
            "target_date": "2026-09-30",
            "existing_asset": "yes",
            "existing_asset_name": "ARGO — extend taxonomy to multi-turn",
            "go_decision": "go",
            "benchmark_status": "confirmed_none",
            "endpoints_probed": ["customer", "sota"],
            "taxonomy_status": "v0_ready",
            "annotator_profile": "decided",
            "endpoint_access": "secured",
            "harness_status": "ready",
            "seed_count": "3",
            "uses_synthetic": "no",
        },
    },
    {
        "name": "Sample, everything blocked",
        "note": "The opposite path. Nothing probed, access blocked, harness unbuilt, no-go pending "
        "discussion. Each stall opens a field naming who owns it — the form makes the blocker "
        "assignable instead of implicit.",
        "type": "sample",
        "state": {
            TYPE_KEY: "sample",
            "urgency": "high",
            "existing_asset": "unchecked",
            "go_decision": "discuss",
            "benchmark_status": "unchecked",
            "endpoints_probed": ["none"],
            "taxonomy_status": "not_started",
            "annotator_profile": "not_started",
            "endpoint_access": "blocked",
            "harness_status": "not_started",
            "uses_synthetic": "yes",
            "synthetic_pct": "65",
        },
    },
    {
        "name": "Pilot",
        "note": "Same three owners and the same header, then an entirely different body — "
        "commercial rather than research. SOW in review asks for both the link and the person "
        "unblocking it.",
        "type": "pilot",
        "state": {
            TYPE_KEY: "pilot",
            "sow_status": "review",
            "expertise": ["swe", "language"],
            "contains_pii": "yes",
            "tooling": "build",
            "uses_synthetic": "no",
        },
    },
    {
        "name": "Production",
        "note": "The longest form. PII opens a categories list, which opens a security review, "
        "which opens an owner — three levels deep. Answering 'no' to PII collapses all of it.",
        "type": "production",
        "state": {
            TYPE_KEY: "production",
            "contract_status": "signed",
            "staffing_model": "dedicated",
            "sla_required": "yes",
            "qa_layer": "yes",
            "contains_pii": "yes",
            "security_review": "in_progress",
            "tooling": "internal",
            "uses_synthetic": "yes",
        },
    },
]


def counts(project_type, state):
    if not project_type:
        return 1, 0
    fields = [f for f in visible_fields(SCHEMA, project_type, state) if f["type"] != "context"]
    conditional = sum(1 for f in fields if f.get("show_if"))
    return len(fields), conditional


# --------------------------------------------------------------------------
# the gate thread
# --------------------------------------------------------------------------

def mrkdwn(s: str) -> str:
    """Just enough of Slack's mrkdwn to read a preview by."""
    out = e(s)
    for token, tag in (("*", "b"), ("_", "i"), ("`", "code")):
        parts = out.split(token)
        out = "".join(
            p if i % 2 == 0 else f"<{tag}>{p}</{tag}>" for i, p in enumerate(parts)
        )
    return out


def render_msg_block(block: Dict[str, Any]) -> str:
    t = block["type"]

    if t == "divider":
        return '<hr class="mrule">'

    if t == "section":
        return f'<div class="msec">{mrkdwn(block["text"]["text"])}</div>'

    if t == "context":
        return f'<div class="mctx">{mrkdwn(block["elements"][0]["text"])}</div>'

    if t == "actions":
        parts = []
        for el in block["elements"]:
            if el["type"] == "checkboxes":
                chosen = {o["value"] for o in el.get("initial_options", [])}
                items = "".join(
                    f'<span class="opt"><i class="check{" on" if o["value"] in chosen else ""}"></i>'
                    f'{mrkdwn(o["text"]["text"])}</span>'
                    for o in el["options"]
                )
                parts.append(f'<div class="opts">{items}</div>')
            elif el["type"] == "button":
                style = " danger" if el.get("style") == "danger" else ""
                parts.append(f'<button class="mbtn{style}">{e(el["text"]["text"])}</button>')
        return f'<div class="macts">{"".join(parts)}</div>'

    return ""


def render_message(title: str, blocks: List[Dict[str, Any]]) -> str:
    body = "".join(render_msg_block(b) for b in blocks)
    return (
        '<div class="msg">'
        f'<div class="mhead"><span class="avatar">PI</span>'
        f'<span class="who">Project Intake</span><span class="bot">APP</span>'
        f'<span class="when">in thread</span></div>'
        f'<div class="mbody">{body}</div>'
        "</div>"
    )


def gate_states() -> List[Dict[str, Any]]:
    """Three points in a run, built by driving the real gate logic."""
    import tempfile

    os.environ["GATES_DB"] = os.path.join(tempfile.mkdtemp(), "preview.db")
    import importlib

    import gates as g

    importlib.reload(g)
    from gate_blocks import checklist_blocks

    owners = {"research_owner": "Tanmay", "delivery_owner": "Shravan", "sales_owner": "Fawaz"}
    g.init_db()

    def new_run():
        rid = g.create_run("C1", "1.1", "Multi-Turn Attack Bench", "sample", owners)
        return rid

    out = []

    rid = new_run()
    out.append(
        {
            "name": "Checklist posted",
            "note": "Lands in a thread under the intake summary the moment a Sample is submitted. "
            "Only Step 2 has controls — everything downstream is locked and says why.",
            "blocks": checklist_blocks(g.get_run(rid)),
        }
    )

    rid = new_run()
    g.set_checked(rid, "seed", [i["key"] for i in g.STEP_BY_KEY["seed"]["items"]])
    g.set_checked(rid, "alignment", [i["key"] for i in g.STEP_BY_KEY["alignment"]["items"]])
    g.sign_off(rid, "alignment", "research_owner", "Tanmay")
    out.append(
        {
            "name": "At the alignment gate",
            "note": "Seed set done, Research has signed. Delivery has not, so Step 4 stays locked — "
            "the checklist's 'never expand past an unvetted seed set', enforced rather than trusted. "
            "The no-go button clears the seed set instead of letting the run limp forward.",
            "blocks": checklist_blocks(g.get_run(rid)),
        }
    )

    rid = new_run()
    for step in g.STEPS:
        g.set_checked(rid, step["key"], [i["key"] for i in step["items"]])
        for owner in step.get("signoffs", []):
            g.sign_off(rid, step["key"], owner, owners[owner])
    st = g.get_run(rid)["state"]
    st["audit_posted_at"] = (
        __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        - __import__("datetime").timedelta(hours=6)
    ).isoformat(timespec="seconds")
    g.save_state(rid, st)
    out.append(
        {
            "name": "Everything ticked, still not shipping",
            "note": "Every box checked, every sign-off in — and ship is still blocked, because the "
            "audit has only been open 6 hours. This is the one rule a paper checklist cannot enforce: "
            "you cannot tick your way past 'never the same day'.",
            "blocks": checklist_blocks(g.get_run(rid)),
        }
    )
    return out


def build_page() -> str:
    cards = []
    for s in STATES:
        conds = condition_map(s["type"]) if s["type"] else {}
        view = build_view(SCHEMA, s["type"], s["state"])
        shown, conditional = counts(s["type"], s["state"])
        label = SCHEMA["project_types"][s["type"]]["label"] if s["type"] else "—"
        cards.append(
            '<section class="card">'
            '<div class="side">'
            f'<h2>{e(s["name"])}</h2>'
            f'<p>{e(s["note"])}</p>'
            '<dl class="stats">'
            f'<div><dt>Type</dt><dd>{e(label)}</dd></div>'
            f'<div><dt>Fields shown</dt><dd>{shown}</dd></div>'
            f'<div><dt>Conditional</dt><dd>{conditional}</dd></div>'
            f'<div><dt>Blocks</dt><dd>{len(view["blocks"])} / 100</dd></div>'
            "</dl>"
            "</div>"
            f'<div class="stage">{render_modal(SCHEMA["title"], view, conds)}</div>'
            "</section>"
        )

    gate_cards = [
        '<div class="band">'
        '<p class="eyebrow">Part two</p>'
        "<h2>The thread after submit</h2>"
        "<p>The modal captures Steps 0 and 1. Steps 2–6 are gates that play out over days, so they "
        "live in a thread under the intake message — one post, rewritten in place as the run "
        "advances. Locked steps render without controls, so an unreachable gate cannot be ticked "
        "by accident.</p>"
        "</div>"
    ]
    for s in gate_states():
        gate_cards.append(
            '<section class="card">'
            '<div class="side">'
            f'<h2>{e(s["name"])}</h2>'
            f'<p>{e(s["note"])}</p>'
            f'<dl class="stats"><div><dt>Blocks</dt><dd>{len(s["blocks"])} / 50</dd></div></dl>'
            "</div>"
            f'<div class="stage">{render_message("Project Intake", s["blocks"])}</div>'
            "</section>"
        )

    return CSS + HEAD + "".join(cards) + "".join(gate_cards) + FOOT


HEAD = """
<header class="masthead">
  <p class="eyebrow">Block Kit review sheet</p>
  <h1>Intake Modal Specimens</h1>
  <p class="lede">Every modal below is rendered from the payload <code>build_view()</code> actually
  sends to Slack — not a mockup. Change <code>form_schema.json</code>, re-run
  <code>preview.py</code>, and these redraw. Fields with a violet spine appeared because of an
  answer above them; the reason is printed under the label.</p>
</header>
"""

FOOT = """
<footer class="foot-note">
  <p>Generated by <code>preview.py</code> from <code>form_schema.json</code>. Spacing and type here
  approximate Slack's modal; exact pixels will differ in the client. What is exact: which fields
  appear, in what order, which are optional, and which re-render the form.</p>
</footer>
"""

CSS = """
<title>Intake Modal Specimens</title>
<style>
:root{
  --ground:#F1EFF4; --surface:#FFFFFF; --sunken:#F7F5F9;
  --ink:#17121C; --ink-2:#453D4E; --muted:#776E82; --faint:#B4AABC;
  --line:#DED7E4; --line-2:#EDE7F1;
  --accent:#5B2D5E; --accent-2:#8A5C8E; --accent-wash:#F3EAF4;
  --blue:#1264A3; --stop:#B23B3B;
  --modal-bg:#FFFFFF; --modal-ink:#1D1C1D; --modal-line:#DDDDDD; --modal-field:#FFFFFF;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --ground:#100D14; --surface:#191420; --sunken:#141019;
    --ink:#EEE9F2; --ink-2:#C3BACB; --muted:#948B9E; --faint:#5F566A;
    --line:#2E2637; --line-2:#241D2C;
    --accent:#C9A0CD; --accent-2:#9E76A3; --accent-wash:#241A27;
    --blue:#6BA8DA; --stop:#E08585;
    --modal-bg:#1A1D21; --modal-ink:#D1D2D3; --modal-line:#35373B; --modal-field:#222529;
  }
}
:root[data-theme="dark"]{
  --ground:#100D14; --surface:#191420; --sunken:#141019;
  --ink:#EEE9F2; --ink-2:#C3BACB; --muted:#948B9E; --faint:#5F566A;
  --line:#2E2637; --line-2:#241D2C;
  --accent:#C9A0CD; --accent-2:#9E76A3; --accent-wash:#241A27;
  --blue:#6BA8DA; --stop:#E08585;
  --modal-bg:#1A1D21; --modal-ink:#D1D2D3; --modal-line:#35373B; --modal-field:#222529;
}

*{box-sizing:border-box}
body{
  margin:0; background:var(--ground); color:var(--ink);
  font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  line-height:1.5; -webkit-font-smoothing:antialiased;
}
code{font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,monospace}

.masthead{max-width:1180px; margin:0 auto; padding:72px 32px 40px}
.eyebrow{
  margin:0 0 14px; font-size:11px; font-weight:700; letter-spacing:.14em;
  text-transform:uppercase; color:var(--accent-2);
}
.masthead h1{
  margin:0 0 18px; font-size:clamp(34px,5vw,52px); font-weight:800;
  letter-spacing:-.03em; text-wrap:balance; color:var(--ink);
}
.lede{margin:0; max-width:64ch; font-size:16px; color:var(--ink-2)}
.lede code{
  font-size:13px; padding:1px 5px; border-radius:4px;
  background:var(--accent-wash); color:var(--accent);
}

.card{
  max-width:1180px; margin:0 auto; padding:36px 32px;
  display:grid; grid-template-columns:minmax(230px,1fr) minmax(0,1.55fr); gap:40px;
  border-top:1px solid var(--line);
}
.card:first-of-type{border-top:none}
.side h2{
  margin:0 0 12px; font-size:19px; font-weight:700; letter-spacing:-.015em; text-wrap:balance;
}
.side p{margin:0 0 22px; font-size:14px; color:var(--muted); max-width:42ch}

.stats{margin:0; display:flex; flex-direction:column; gap:1px; background:var(--line-2);
  border:1px solid var(--line-2); border-radius:8px; overflow:hidden}
.stats > div{display:flex; justify-content:space-between; gap:12px; padding:8px 12px;
  background:var(--surface)}
.stats dt{font-size:11px; letter-spacing:.06em; text-transform:uppercase; color:var(--faint)}
.stats dd{margin:0; font-size:13px; font-weight:650; font-variant-numeric:tabular-nums;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--ink-2)}

.stage{min-width:0}

/* --- Slack modal --- */
.modal{
  background:var(--modal-bg); color:var(--modal-ink); border:1px solid var(--modal-line);
  border-radius:10px; overflow:hidden; box-shadow:0 12px 34px rgba(20,10,26,.14);
  font-size:15px;
}
.bar{
  display:flex; align-items:center; justify-content:space-between; gap:12px;
  padding:15px 18px; border-bottom:1px solid var(--modal-line);
}
.bar .t{font-weight:800; font-size:17px; letter-spacing:-.01em}
.bar .x{color:var(--faint)}
.body{padding:16px 18px; max-height:560px; overflow-y:auto;
  display:flex; flex-direction:column; gap:15px}
.foot{
  display:flex; justify-content:flex-end; gap:9px;
  padding:13px 18px; border-top:1px solid var(--modal-line);
}
.btn{
  font:inherit; font-size:14px; font-weight:700; padding:8px 15px;
  border-radius:5px; border:1px solid var(--modal-line);
  background:transparent; color:var(--modal-ink); cursor:default;
}
.btn.go{background:#007A5A; border-color:#007A5A; color:#fff}
.btn.go.dis{background:transparent; border-color:var(--modal-line); color:var(--faint)}

.rule{border:none; border-top:1px solid var(--modal-line); margin:2px 0}
.sect{
  display:flex; align-items:baseline; gap:10px; flex-wrap:wrap;
  font-size:11px; font-weight:700; letter-spacing:.1em; text-transform:uppercase;
  color:var(--accent-2); padding-top:6px;
}

.aside{font-size:13px; font-style:italic; color:var(--muted)}

.row{display:flex; flex-direction:column; gap:6px}
.row.cond{
  border-left:2px solid var(--accent-2); padding-left:13px; margin-left:-15px;
}
.lab{display:flex; align-items:center; gap:8px; flex-wrap:wrap; font-weight:700; font-size:14px}
.lab code{font-weight:400; font-size:11px; color:var(--faint)}
.tag{
  font-size:10px; font-weight:700; letter-spacing:.05em; text-transform:uppercase;
  padding:2px 6px; border-radius:3px; border:1px solid var(--modal-line); color:var(--muted);
}
.tag.trig{border-color:var(--accent-2); color:var(--accent-2)}
.why-line{font-size:11.5px; color:var(--accent-2); margin-top:-2px}
.why-line b{font-weight:700}
.why{font-size:10px; letter-spacing:0; text-transform:none; color:var(--accent-2); opacity:.8}
.hint{font-size:12px; color:var(--muted)}

.field{
  display:flex; align-items:center; gap:8px; flex-wrap:wrap;
  min-height:36px; padding:7px 11px; border-radius:5px;
  border:1px solid var(--modal-line); background:var(--modal-field); font-size:14px;
}
.field.select{justify-content:flex-start}
.field .caret{margin-left:auto; color:var(--faint)}
.field .ico{color:var(--faint)}
.ph{color:var(--faint)}
.val{color:var(--modal-ink)}
.bound{
  margin-left:auto; font-size:11px; font-family:ui-monospace,Menlo,monospace;
  color:var(--faint); font-variant-numeric:tabular-nums;
}
.chip{
  padding:2px 8px; border-radius:3px; font-size:13px;
  background:var(--accent-wash); color:var(--accent); border:1px solid var(--line);
}
.opts{display:flex; flex-direction:column; gap:7px; padding-top:2px}
.opt{display:flex; align-items:center; gap:9px; font-size:14px}
.opt i{
  width:15px; height:15px; flex:0 0 15px; display:inline-block;
  border:1.5px solid var(--faint); background:transparent;
}
.opt i.radio{border-radius:50%}
.opt i.check{border-radius:3px}
.opt i.on{border-color:#1264A3; background:#1264A3; box-shadow:inset 0 0 0 3px var(--modal-field)}
.opt i.check.on{box-shadow:inset 0 0 0 2px var(--modal-field)}

/* --- part two: the gate thread --- */
.band{
  max-width:1180px; margin:0 auto; padding:60px 32px 8px; border-top:1px solid var(--line);
}
.band h2{margin:0 0 14px; font-size:clamp(26px,3.6vw,36px); font-weight:800; letter-spacing:-.025em}
.band p{margin:0; max-width:64ch; font-size:15px; color:var(--ink-2)}

.msg{
  background:var(--modal-bg); color:var(--modal-ink);
  border:1px solid var(--modal-line); border-radius:10px; overflow:hidden;
  box-shadow:0 12px 34px rgba(20,10,26,.14); font-size:15px;
}
.mhead{
  display:flex; align-items:center; gap:8px; padding:13px 18px 4px; flex-wrap:wrap;
}
.avatar{
  width:22px; height:22px; border-radius:5px; background:var(--accent);
  color:#fff; font-size:10px; font-weight:800; display:grid; place-items:center;
}
.who{font-weight:800; font-size:15px}
.bot{
  font-size:9px; font-weight:700; letter-spacing:.06em; padding:2px 4px; border-radius:3px;
  background:var(--modal-line); color:var(--muted);
}
.when{font-size:12px; color:var(--faint)}
.mbody{
  padding:6px 18px 18px; max-height:620px; overflow-y:auto;
  display:flex; flex-direction:column; gap:9px;
}
.msec{font-size:15px; line-height:1.45}
.mctx{font-size:12.5px; color:var(--muted); line-height:1.45}
.mctx code{font-size:12px; letter-spacing:.08em}
.mrule{border:none; border-top:1px solid var(--modal-line); margin:5px 0}
.macts{display:flex; flex-wrap:wrap; gap:8px; align-items:center}
.mbtn{
  font:inherit; font-size:13px; font-weight:700; padding:7px 13px; border-radius:4px;
  border:1px solid var(--modal-line); background:transparent; color:var(--modal-ink); cursor:default;
}
.mbtn.danger{border-color:var(--stop); color:var(--stop)}

.foot-note{
  max-width:1180px; margin:0 auto; padding:32px; border-top:1px solid var(--line);
}
.foot-note p{margin:0; max-width:70ch; font-size:13px; color:var(--muted)}
.foot-note code{font-size:12px; color:var(--ink-2)}

@media (max-width:860px){
  .card{grid-template-columns:1fr; gap:24px; padding:28px 20px}
  .masthead{padding:48px 20px 28px}
  .row.cond{margin-left:0}
}
</style>
"""

if __name__ == "__main__":
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(build_page())
    print(f"wrote {OUT}")
