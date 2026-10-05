"""Offline checks on the view payloads — no Slack connection needed.

Run: python test_form.py
"""

import json

from form_builder import (
    TYPE_KEY,
    build_view,
    extract_state,
    is_input,
    load_schema,
    trigger_keys,
    valid_initial,
    visible_fields,
)

schema = load_schema()
failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail and not cond else ''}")
    if not cond:
        failures.append(name)


def block_ids(project_type, state):
    return [b.get("block_id") for b in build_view(schema, project_type, state)["blocks"]]


# 0. schema integrity — a duplicate key across common + a type would collide as a
#    Slack block_id and silently drop one of the fields
common_keys = [f["key"] for f in schema["common_fields"]]
check("common keys unique", len(common_keys) == len(set(common_keys)))
for name, defn in schema["project_types"].items():
    keys = common_keys + [f["key"] for f in defn["fields"]]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    check(f"{name} has no duplicate keys", not dupes, str(dupes))
    check(f"{name} does not shadow the type picker", TYPE_KEY not in keys)
for name in schema["project_types"]:
    for f in visible_fields(schema, name, {}) + schema["common_fields"]:
        cond = f.get("show_if")
        if cond:
            known = set(common_keys) | {x["key"] for x in schema["project_types"][name]["fields"]}
            check(
                f"{name}.{f['key']} show_if targets a real field",
                cond["field"] in known or cond["field"] == TYPE_KEY,
                cond["field"],
            )

# 0b. labels must map back to keys one-to-one — the sheet header shows labels and
#     reads them back as keys, so a clash would silently mis-file a column
all_fields = list(schema["common_fields"])
for _defn in schema["project_types"].values():
    all_fields += _defn["fields"]
pairs = {(f["key"], f["label"]) for f in all_fields if f["type"] != "context"}
by_label = {}
for _k, _l in pairs:
    by_label.setdefault(_l, set()).add(_k)
clashes = {l: sorted(ks) for l, ks in by_label.items() if len(ks) > 1}
check("labels are unique across the schema", not clashes, str(clashes))

# 0c. the form has to stay short enough that people fill it honestly
for name in schema["project_types"]:
    state = {TYPE_KEY: name, "intake_stage": "scoping"}
    vis = [f for f in visible_fields(schema, name, state) if f["type"] != "context"]
    req = [f for f in vis if not f.get("optional")]
    check(f"{name} asks for at most 12 required fields", len(req) <= 12, str(len(req)))
    print(f"      {name}: {len(req)} required, {len(vis)} visible when scoping")

# 0d. nothing started means the research questions stay out of the way
scoping = block_ids("sample", {TYPE_KEY: "sample", "intake_stage": "scoping"})
underway = block_ids("sample", {TYPE_KEY: "sample", "intake_stage": "underway"})
check("scoping hides the Step 1 block", "step1_header" not in scoping)
check("scoping hides research status", "taxonomy_status" not in scoping)
check("scoping keeps the Step 0 gate", "go_decision" in scoping and "existing_asset" in scoping)
check("underway reveals Step 1", "taxonomy_status" in underway and "step1_header" in underway)
check("stage gate cuts the form roughly in half", len(scoping) * 1.6 < len(underway),
      f"{len(scoping)} vs {len(underway)}")

# 0e. requiredness scales with the stage. Claiming research is underway is a
#     claim that the research answers exist; claiming readiness is a bigger one.
from form_builder import is_required  # noqa: E402

def required_at(stage):
    """Input fields that must be answered at this stage. Headers are not fields."""
    st = {TYPE_KEY: "sample", "intake_stage": stage}
    return {
        f["key"]
        for f in visible_fields(schema, "sample", st)
        if is_input(f) and is_required(f, st)
    }


scoping_req, underway_req, ready_req = (required_at(s) for s in ("scoping", "underway", "ready"))

check("scoping requires only identity and the Step 0 gate", len(scoping_req) == 11, str(len(scoping_req)))
check("scoping requires no research answers", "failure_pattern" not in scoping_req)
check("underway requires the research answers",
      {"failure_pattern", "taxonomy_status", "benchmark_status", "endpoints_probed"} <= underway_req)
check("underway does not require delivery readiness", "harness_status" not in underway_req)
check("ready requires delivery and engineering too",
      {"annotator_profile", "guideline_v0", "lifecycle_mapped", "endpoint_access",
       "harness_status", "seed_count", "uses_synthetic"} <= ready_req)
check("requirements only ever grow", scoping_req <= underway_req <= ready_req)
check("underway and ready are no longer identical", underway_req != ready_req)
check("optional extras stay optional at every stage",
      not any("spec_doc_link" in r for r in (scoping_req, underway_req, ready_req)))
check("pipeline_relevance stays optional", "pipeline_relevance" not in ready_req)
print(f"      required: {len(scoping_req)} scoping → {len(underway_req)} underway → {len(ready_req)} ready")

# the block Slack renders has to agree with is_required, or the client and the
# submit check would disagree about what is mandatory
for stage in ("scoping", "underway", "ready"):
    st = {TYPE_KEY: "sample", "intake_stage": stage}
    view = build_view(schema, "sample", st)
    fields = {f["key"]: f for f in visible_fields(schema, "sample", st)}
    mismatched = [
        b["block_id"]
        for b in view["blocks"]
        if b.get("type") == "input"
        and b["block_id"] in fields
        and b.get("optional") == is_required(fields[b["block_id"]], st)
    ]
    check(f"{stage}: rendered blocks match the required set", not mismatched, str(mismatched))

# a required_if controller must re-render, or the change never reaches the form
check("required_if controllers are triggers", "intake_stage" in trigger_keys(schema))

# 1. empty modal: picker only, no submit button (nothing to submit yet)
v = build_view(schema, None, {})
check("empty view has picker", v["blocks"][0]["block_id"] == TYPE_KEY)
check("empty view has no submit", "submit" not in v)
check("picker dispatches", v["blocks"][0].get("dispatch_action") is True)

# 2. picking a type loads that type's fields
v = build_view(schema, "sample", {TYPE_KEY: "sample", "intake_stage": "underway"})
ids = [b.get("block_id") for b in v["blocks"]]
check("sample form loads", "failure_pattern" in ids and "taxonomy_status" in ids)
check("three owners load", {"research_owner", "delivery_owner", "sales_owner"} <= set(ids))
check("step headers render", "step0_header" in ids and "step1_header" in ids)
check("submit appears", "submit" in v)

# 3. section headers are context blocks, not inputs
header = next(b for b in v["blocks"] if b.get("block_id") == "step0_header")
check("header is a context block", header["type"] == "context")
check("header carries no element", "element" not in header)
check("is_input rejects headers", not is_input({"type": "context", "key": "x"}))

# 4. conditional reveal, and non-matching branch stays hidden
ids = block_ids("sample", {"existing_asset": "yes"})
check("existing_asset_name revealed", "existing_asset_name" in ids)
ids = block_ids("sample", {"existing_asset": "no"})
check("existing_asset_name hidden", "existing_asset_name" not in ids)

ids = block_ids("sample", {"go_decision": "discuss"})
check("decision_notes on 'needs discussion'", "decision_notes" in ids)
ids = block_ids("sample", {"go_decision": "go"})
check("decision_notes hidden on 'go'", "decision_notes" not in ids)

ids = block_ids("sample", {"intake_stage": "underway", "uses_synthetic": "yes"})
check("synthetic chain revealed", "synthetic_pct" in ids and "hitl_role" in ids)

ids = block_ids("sample", {"intake_stage": "underway", "endpoint_access": "blocked"})
check("access blocker owner revealed", "access_blocker_owner" in ids)

# 5. multi_select as a controller (any selected value matches)
ids = block_ids("sample", {"intake_stage": "underway", "endpoints_probed": ["none"]})
check("multi_select controller reveals", "probe_blocker" in ids)
ids = block_ids("sample", {"intake_stage": "underway", "endpoints_probed": ["customer", "sota"]})
check("multi_select controller hides", "probe_blocker" not in ids)

# 6. chained conditional — security_review is itself gated by contains_pii
ids = block_ids("production", {"contains_pii": "yes", "security_review": "none"})
check("chain depth 2 revealed", "security_review" in ids and "security_owner" in ids)
ids = block_ids("production", {"contains_pii": "no", "security_review": "none"})
check("hidden controller hides its child", "security_owner" not in ids)

# 7. the entire form swaps between types
sets = {t: set(block_ids(t, {})) for t in schema["project_types"]}
check("sample vs production differ", len(sets["sample"] ^ sets["production"]) > 15)
check("pilot vs production differ", len(sets["pilot"] ^ sets["production"]) > 8)

# 8. typed answers survive a re-render
state = {
    TYPE_KEY: "sample",
    "project_name": "Multi-Turn Attack Bench",
    "account": "Tencent",
    "workstream": "red_team",
    "urgency": "critical",
    "intake_stage": "underway",
    "existing_asset": "yes",
    "existing_asset_name": "ARGO",
    "seed_count": "3",
    "target_date": "2026-09-30",
    "research_owner": "U0RESEARCH",
    "endpoints_probed": ["customer", "sota"],
}
v = build_view(schema, "sample", state)
by_id = {b.get("block_id"): b for b in v["blocks"] if b.get("type") == "input"}
check("text round-trips", by_id["project_name"]["element"]["initial_value"] == "Multi-Turn Attack Bench")
check("select round-trips", by_id["urgency"]["element"]["initial_option"]["value"] == "critical")
check("radio round-trips", by_id["existing_asset"]["element"]["initial_option"]["value"] == "yes")
check("number round-trips", by_id["seed_count"]["element"]["initial_value"] == "3")
check("date round-trips", by_id["target_date"]["element"]["initial_date"] == "2026-09-30")
check("user round-trips", by_id["research_owner"]["element"]["initial_user"] == "U0RESEARCH")
check("type picker round-trips", by_id[TYPE_KEY]["element"]["initial_option"]["value"] == "sample")
check(
    "multi-select round-trips",
    [o["value"] for o in by_id["endpoints_probed"]["element"]["initial_options"]]
    == ["customer", "sota"],
)
check("urgency justification revealed", "urgency_justification" in by_id)

# 9. number bounds reach the element as strings, per Block Kit
check("min_value applied", by_id["seed_count"]["element"]["min_value"] == "2")
check("max_value applied", by_id["seed_count"]["element"]["max_value"] == "5")
synth = {
    b.get("block_id"): b
    for b in build_view(
        schema, "sample", {"intake_stage": "underway", "uses_synthetic": "yes"}
    )["blocks"]
}
check("synthetic capped at 70", synth["synthetic_pct"]["element"]["max_value"] == "70")

# 10. triggers are derived, and only controllers dispatch
triggers = trigger_keys(schema)
expected = {
    "urgency", "workstream", "existing_asset", "go_decision", "benchmark_status",
    "endpoints_probed", "taxonomy_status", "annotator_profile", "endpoint_access",
    "harness_status", "uses_synthetic", "sow_status", "expertise", "contains_pii",
    "tooling", "staffing_model", "sla_required", "qa_layer", "security_review",
    "contract_status",
}
check("triggers derived", expected <= triggers, str(sorted(expected - triggers)))
check("non-controller does not dispatch", "dispatch_action" not in by_id["project_name"])
check("controller dispatches", by_id["existing_asset"].get("dispatch_action") is True)

# 10b. half-typed URLs must not be sent back as initial_value.
#      Slack rejects the entire views.update if a url_text_input's initial_value
#      does not parse, which kills the modal mid-form. Regression test for that.
check("full url is valid", valid_initial("url", "https://docs.google.com/d/1"))
check("http url is valid", valid_initial("url", "http://example.com"))
check("bare domain is not", not valid_initial("url", "docs.google.com/d/1"))
check("scheme alone is not", not valid_initial("url", "https://"))
check("half-typed is not", not valid_initial("url", "http"))
check("empty is not", not valid_initial("url", ""))
check("email valid", valid_initial("email", "a@b.co"))
check("email half-typed is not", not valid_initial("email", "a@b"))
check("plain text is always fine", valid_initial("text", "anything at all"))

partial = build_view(schema, "sample", {TYPE_KEY: "sample", "intake_stage": "underway", "spec_doc_link": "docs.google.com/x"})
spec = next(b for b in partial["blocks"] if b.get("block_id") == "spec_doc_link")
check("partial url is not sent as initial_value", "initial_value" not in spec["element"])
check("user is told it was cleared", "Cleared" in spec.get("hint", {}).get("text", ""),
      spec.get("hint", {}).get("text", ""))
check("hint names the fix", "https://example.com" in spec.get("hint", {}).get("text", ""))

good = build_view(schema, "sample", {TYPE_KEY: "sample", "intake_stage": "underway", "spec_doc_link": "https://x.test/spec"})
spec = next(b for b in good["blocks"] if b.get("block_id") == "spec_doc_link")
check("valid url round-trips", spec["element"]["initial_value"] == "https://x.test/spec")
check("valid url has no cleared note", "Cleared" not in spec.get("hint", {}).get("text", ""))

# every url field in every type must survive a half-typed value
for name in schema["project_types"]:
    url_fields = [
        f["key"]
        for f in visible_fields(schema, name, {TYPE_KEY: name, "intake_stage": "underway"})
        if f["type"] == "url"
    ]
    st = {TYPE_KEY: name, "intake_stage": "underway"}
    st.update({k: "partial.link/x" for k in url_fields})
    view = build_view(schema, name, st)
    bad = [
        b["block_id"]
        for b in view["blocks"]
        if b.get("type") == "input" and b["element"].get("type") == "url_text_input"
        and "initial_value" in b["element"]
        and "://" not in b["element"]["initial_value"]
    ]
    check(f"{name} sends no invalid urls", not bad, str(bad))

# 11. state extraction from a Slack-shaped payload
fake_view = {
    "state": {
        "values": {
            "project_name": {"project_name": {"type": "plain_text_input", "value": "X"}},
            "endpoints_probed": {
                "endpoints_probed": {
                    "type": "multi_static_select",
                    "selected_options": [{"value": "customer"}, {"value": "sota"}],
                }
            },
            "pii_categories": {
                "pii_categories": {"type": "checkboxes", "selected_options": [{"value": "names"}]}
            },
            "research_owner": {"research_owner": {"type": "users_select", "selected_user": "U123"}},
            "target_date": {"target_date": {"type": "datepicker", "selected_date": "2026-01-01"}},
            "spec_doc_link": {"spec_doc_link": {"type": "url_text_input", "value": "https://x.test/s"}},
            "seed_count": {"seed_count": {"type": "number_input", "value": "3"}},
            "empty": {"empty": {"type": "plain_text_input", "value": None}},
        }
    }
}
got = extract_state(fake_view)
check("extract text", got["project_name"] == "X")
check("extract multi", got["endpoints_probed"] == ["customer", "sota"])
check("extract checkboxes", got["pii_categories"] == ["names"])
check("extract user", got["research_owner"] == "U123")
check("extract date", got["target_date"] == "2026-01-01")
check("extract url", got["spec_doc_link"] == "https://x.test/s")
check("extract number", got["seed_count"] == "3")
check("empty dropped", "empty" not in got)

# 12. every type renders and stays inside the modal block budget, fully expanded
def widest_state(name):
    """The answers that reveal the most fields, not merely the first option.

    Seeding every controller with options[0] pinned intake_stage to "scoping",
    which hides ~25 fields — so the budget was being measured against the form
    at its smallest, and the 100-block ceiling was never exercised.
    """
    state = {TYPE_KEY: name}
    for _ in range(4):  # repeat so chained conditionals open up in turn
        for f in visible_fields(schema, name, state):
            if not f.get("options") or f["key"] in state:
                continue
            if f["type"] in ("multi_select", "checkboxes"):
                state[f["key"]] = [o["value"] for o in f["options"]]
                continue
            best, best_count = f["options"][0]["value"], -1
            for option in f["options"]:
                trial = {**state, f["key"]: option["value"]}
                count = len(visible_fields(schema, name, trial))
                if count > best_count:
                    best, best_count = option["value"], count
            state[f["key"]] = best
    return state


for name in schema["project_types"]:
    full = widest_state(name)
    view = build_view(schema, name, full)
    json.dumps(view)
    n = len(view["blocks"])
    shown = len([f for f in visible_fields(schema, name, full) if f["type"] != "context"])
    check(f"{name} under 100 blocks at its widest", n < 100, str(n))
    print(f"      {name}: {n} blocks / {shown} fields at maximum expansion")

check("the widest sample really is the expanded one",
      widest_state("sample").get("intake_stage") in ("underway", "ready"),
      str(widest_state("sample").get("intake_stage")))

print()
print(f"{len(failures)} failure(s)" if failures else "all checks passed")
raise SystemExit(1 if failures else 0)
