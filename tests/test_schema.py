"""Tests for the versioned PlanState / IndexState schema (reckon/_schema.py).

Covers:
  - the canonical-dump shape contract (model_dump(exclude_unset) == read_state)
  - state-level round-trip (read_state(write_state(html, state)) == state)
  - byte-identity of regenerated reckon-owned sections through the typed path
  - lenient normalisations (med→mid, doc→research, status union, derived
    statuses, dropped unknown attrs) and docs-project capture
  - strict validate_for_write (required-on-write + non-empty followup prompt)
  - the committed plan.schema.json == gen_json_schema()
  - cross-project conformance scan (skip-if-mount-absent; xfail-catalogue)

This file owns the canonical round-trip test:
``tests/test_schema.py::test_state_round_trip``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reckon._plan_html import from_html, read_state, to_html, write_state
from reckon.capability import CAPABILITY_CLASSES, CAPABILITY_SCHEMA_VERSION
from reckon._schema import (
    EFFORT_ENUM,
    PlanState,
    ROI_ENUM,
    STATUS_ENUM,
    IndexData,
    IndexState,
    gen_json_schema,
    schema_path,
)

# ── Fixtures / sample plan HTML ──────────────────────────────────────────────

SPARSE_PLAN = (
    '<!doctype html>\n<html lang="en">\n<head>\n'
    '<meta charset="utf-8">\n'
    '<meta name="docs-project" content="reckon">\n'
    "<title>Bare plan | reckon</title>\n"
    '</head>\n<body>\n<main class="plan-doc"></main>\n</body>\n</html>\n'
)

FULL_PLAN = (
    '<!doctype html>\n<html lang="en">\n<head>\n'
    '<meta charset="utf-8">\n'
    '<meta name="docs-project" content="reckon">\n'
    '<meta name="reckon-type" content="plan">\n'
    '<meta name="plan-slug" content="sample">\n'
    '<meta name="plan-status" content="active">\n'
    '<meta name="plan-impl" content="0.5">\n'
    '<meta name="plan-version" content="3">\n'
    '<meta name="plan-roi" content="high">\n'
    '<meta name="plan-effort" content="L">\n'
    '<meta name="plan-milestone" content="M2">\n'
    '<meta name="plan-sprint" content="S4">\n'
    '<meta name="plan-tier" content="opus">\n'
    '<meta name="plan-summary" content="a sample plan">\n'
    '<meta name="plan-owner" content="smc">\n'
    '<meta name="plan-modified" content="2026-05-29">\n'
    '<meta name="plan-depends-on" content="dep-a,dep-b">\n'
    '<meta name="plan-blocks" content="blk-c">\n'
    "<title>Sample | reckon</title>\n"
    '</head>\n<body>\n<main class="plan-doc">\n'
    '<section data-reckon="decisions" id="decisions" class="r-decisions">\n'
    '<h2><span class="sec">§</span> Decisions</h2>\n'
    '<div class="r-dec" data-key="locked-one" data-choice="build"'
    ' data-by="smc" data-when="2026-05-27">\n'
    '<p class="r-dec-q">Pick a verb</p>\n'
    '<p class="r-dec-opts">\n'
    '<button class="r-opt chosen" data-value="build">build</button>\n'
    '<button class="r-opt" data-value="export">export</button>\n'
    "</p>\n"
    '<p class="r-dec-rat">because</p>\n</div>\n'
    '<div class="r-dec" data-key="open-one" data-choice=""'
    ' data-by="" data-when="">\n'
    '<p class="r-dec-q">An open question</p>\n'
    '<p class="r-dec-rat"></p>\n</div>\n'
    '<div class="r-dec" data-key="freeform-one" data-choice="my typed answer"'
    ' data-by="smc" data-when="2026-05-28">\n'
    '<p class="r-dec-q">Free form</p>\n'
    '<p class="r-dec-rat">rationale here</p>\n</div>\n'
    "</section>\n"
    '<section data-reckon="followups" id="followups" class="r-followups">\n'
    '<h2><span class="sec">§</span> Followups</h2>\n'
    '<article class="r-fu" data-id="f1" data-status="open" data-tier="sonnet"'
    ' data-written-by="smc" data-written-at="2026-05-27"'
    ' data-recommends-skill="/reckon-ship sample"'
    ' data-resolved-at="" data-resolved-by="">\n'
    '<h4 class="r-fu-title">Open followup</h4>\n'
    '<div class="r-fu-body">do the thing</div>\n'
    '<pre class="r-fu-prompt">§05 prompt text</pre>\n</article>\n'
    '<article class="r-fu" data-id="f2" data-status="open" data-tier="opus"'
    ' data-written-by="smc" data-written-at="2026-05-26"'
    ' data-recommends-skill=""'
    ' data-resolved-at="2026-05-28" data-resolved-by="claude">\n'
    '<h4 class="r-fu-title">Resolved followup</h4>\n'
    '<div class="r-fu-body">was done</div>\n'
    '<pre class="r-fu-prompt">old prompt</pre>\n'
    '<p class="r-fu-outcome">landed it</p>\n</article>\n'
    "</section>\n"
    '<section data-reckon="questions" id="questions" class="r-questions">\n'
    '<h2><span class="sec">§</span> Open questions</h2>\n'
    '<div class="r-q" data-id="q1" data-section="s1" data-status="open"'
    ' data-opened-by="smc" data-opened-at="2026-05-01"'
    ' data-resolved-at="" data-resolved-by="">\n'
    '<p class="r-q-body">an open question</p>\n</div>\n'
    '<div class="r-q" data-id="q2" data-section="s2" data-status="resolved"'
    ' data-opened-by="smc" data-opened-at="2026-05-02"'
    ' data-resolved-at="2026-05-10" data-resolved-by="smc">\n'
    '<p class="r-q-body">a resolved question</p>\n'
    '<p class="r-q-resolution">the answer</p>\n</div>\n'
    "</section>\n"
    '<section data-reckon="research" id="research" class="r-research-list">\n'
    '<h2><span class="sec">§</span> Research</h2>\n'
    '<div class="r-research" data-id="r1" data-type="paper" data-source="arxiv"'
    ' data-added-by="smc" data-when="2026-05-01" data-url="https://x">\n'
    '<span class="r-research-title"><a href="https://x">A title</a></span></div>\n'
    "</section>\n"
    '<section data-reckon="comments" id="comments" class="r-comments">\n'
    '<div class="r-comment" data-section="s1" data-id="c1" data-who="smc"'
    ' data-when="2026-05-27" data-quote="anchor">\n'
    '<div class="r-comment-body">a comment</div>\n</div>\n'
    "</section>\n"
    "</main>\n</body>\n</html>\n"
)


# ── 1. Canonical-dump shape contract ─────────────────────────────────────────


@pytest.mark.parametrize("html", [SPARSE_PLAN, FULL_PLAN], ids=["sparse", "full"])
def test_canonical_dump_equals_read_state(html):
    """model_validate(read_state).model_dump(exclude_unset) == read_state, at
    every nesting level. This is the keystone diagnostic: if it holds, the
    write_state byte-identity falls out for free."""
    st = read_state(html)
    dump = PlanState.model_validate(st).canonical_dump()
    assert dump == st


def test_canonical_dump_no_default_injection_on_sparse():
    """A sparse plan must NOT gain default scalars (effort=M, tier=sonnet, …)
    in the canonical dump — that would balloon <head> on every save."""
    dump = from_html(SPARSE_PLAN).canonical_dump()
    for k in ("effort", "tier", "milestone", "roi", "owner", "status"):
        assert k not in dump, f"{k} leaked into sparse dump via default injection"


# ── 2. Byte-identity of regenerated sections through the typed path ──────────


@pytest.mark.parametrize("html", [SPARSE_PLAN, FULL_PLAN], ids=["sparse", "full"])
def test_typed_path_byte_identical_to_dict_path(html):
    """write_state(html, from_html(html).canonical_dump()) is byte-identical to
    write_state(html, read_state(html)). The typed layer changes nothing."""
    dict_path = write_state(html, read_state(html))
    typed_path = to_html(html, from_html(html))
    assert typed_path == dict_path


def test_byte_identity_on_real_reckon_plans():
    """Same byte-identity on the repo's own (already-normalised) plans."""
    docs = Path(__file__).resolve().parent.parent / "docs"
    plans = [
        p
        for p in sorted(docs.glob("*.html"))
        if p.name not in {"index.html", "home.html"}
    ]
    assert plans, "expected reckon plans to exist"
    for p in plans:
        html = p.read_text(encoding="utf-8")
        assert to_html(html, from_html(html)) == write_state(html, read_state(html)), (
            p.name
        )


# ── 3. State-level round-trip ───────────────────────────────────────────────


def test_state_round_trip():
    """read_state(write_state(html, state)) == state for a representative state:
    resolved + open followups, locked + open + free-form decisions, section-
    keyed comments, questions, and all scalars incl. server-owned ones.

    This is the canonical state round-trip contract.
    """
    state = read_state(FULL_PLAN)
    # sanity: the fixture exercises every section
    assert len(state["decisions"]) == 3
    assert len(state["followups"]) == 2
    assert len(state["questions"]) == 2
    assert state["research"] and state["comments"]
    assert state["impl"] == 0.5 and state["version"] == 3  # server-owned scalars

    rendered = write_state(FULL_PLAN, state)
    expected = {k: v for k, v in state.items() if k != "compatibility_warnings"}
    assert read_state(rendered) == expected

    # And the typed wrappers must agree: read_state(to_html(html, ps)) == state
    ps = from_html(FULL_PLAN)
    assert read_state(to_html(FULL_PLAN, ps)) == expected


def test_round_trip_idempotent_second_pass():
    """A second write must reproduce the first byte-for-byte (no drift)."""
    once = write_state(FULL_PLAN, read_state(FULL_PLAN))
    twice = write_state(once, read_state(once))
    assert once == twice


# ── 4. Lenient normalisations + docs-project capture ────────────────────────


def test_roi_med_normalised_to_mid():
    ps = PlanState.model_validate({"roi": "med"})
    assert ps.roi == "mid"


def test_type_doc_normalised_to_research():
    ps = PlanState.model_validate({"type": "doc"})
    assert ps.type == "research"


def test_type_empty_defaults_to_plan():
    assert PlanState.model_validate({"type": ""}).type == "plan"


@pytest.mark.parametrize("status", STATUS_ENUM)
def test_status_union_accepted(status):
    assert PlanState.model_validate({"status": status}).status == status


def test_off_enum_status_does_not_raise():
    """Lenient read: an off-enum status from an old plan must NOT raise."""
    ps = PlanState.model_validate({"status": "weird-legacy-status"})
    assert ps.status == "weird-legacy-status"


def test_followup_status_derived_from_resolved_at():
    fu = {"id": "f", "resolved_at": "2026-05-01"}  # stale/absent literal status
    ps = PlanState.model_validate({"followups": [fu]})
    assert ps.followups[0].status == "resolved"


def test_question_status_derived_property_not_field():
    """Question has no status FIELD (read_state omits it) — only a property.
    A status field would force-include and break the round-trip."""
    ps = PlanState.model_validate(
        {"questions": [{"id": "q", "resolved_at": "2026-05-01"}]}
    )
    assert ps.questions[0].status == "resolved"
    assert "status" not in ps.questions[0].model_dump()


def test_unknown_attrs_dropped():
    """extra='ignore': stray keys (e.g. plan-project, ambix followup extras)
    drop cleanly and never appear in the dump."""
    ps = PlanState.model_validate(
        {
            "status": "active",
            "bogus_scalar": "x",
            "followups": [
                {"id": "f", "data_opened_by": "x", "section": "y", "prompt": "p"}
            ],
        }
    )
    assert "bogus_scalar" not in ps.model_dump()
    assert "data_opened_by" not in ps.followups[0].model_dump()
    assert "section" not in ps.followups[0].model_dump()


def test_docs_project_captured_on_read():
    assert read_state(SPARSE_PLAN).get("project") == "reckon"
    assert from_html(SPARSE_PLAN).project == "reckon"


def test_docs_project_absent_defaults_empty():
    no_proj = SPARSE_PLAN.replace('<meta name="docs-project" content="reckon">\n', "")
    assert "project" not in read_state(no_proj)
    assert from_html(no_proj).project == ""


def test_write_state_ignores_project_meta():
    """write_state must NOT emit a docs-project meta (read-captures/write-ignores
    asymmetry). The authored meta survives untouched in the source."""
    ps = from_html(SPARSE_PLAN)
    out = to_html(SPARSE_PLAN, ps)
    # exactly one docs-project meta (the authored one), not a duplicated one
    assert out.count('name="docs-project"') == 1


# ── 5. Strict validate_for_write (the reject path) ──────────────────────────


def test_validate_for_write_accepts_complete_state():
    ps = PlanState(project="reckon", slug="x", title="X", status="active")
    assert ps.validate_for_write() is ps


@pytest.mark.parametrize("missing", ["project", "slug", "title", "status"])
def test_validate_for_write_rejects_missing_required(missing):
    kw = {"project": "reckon", "slug": "x", "title": "X", "status": "active"}
    kw[missing] = ""
    with pytest.raises(ValueError, match=missing):
        PlanState(**kw).validate_for_write()


def test_validate_for_write_rejects_empty_followup_prompt():
    ps = PlanState(
        project="reckon",
        slug="x",
        title="X",
        status="active",
        followups=[{"id": "f1", "prompt": ""}],
    )
    with pytest.raises(ValueError, match="prompt"):
        ps.validate_for_write()


def test_validate_for_write_rejects_off_enum():
    ps = PlanState(project="reckon", slug="x", title="X", status="not-a-status")
    with pytest.raises(ValueError, match="status"):
        ps.validate_for_write()


def test_validate_for_write_accepts_nonempty_followup_prompt():
    ps = PlanState(
        project="reckon",
        slug="x",
        title="X",
        status="active",
        followups=[{"id": "f1", "prompt": "do this"}],
    )
    assert ps.validate_for_write() is ps


# ── 6. Decisions list view (derived, never stored) ──────────────────────────


def test_decisions_list_derives_chosen_from_choice():
    ps = from_html(FULL_PLAN)
    rows = ps.decisions_list()
    by_key = {r["key"]: r for r in rows}
    assert by_key["locked-one"]["chosen"] == "build"
    assert by_key["open-one"]["chosen"] == ""
    # chosen is NOT stored on the model
    assert "chosen" not in ps.decisions["locked-one"].model_dump()


# ── 7. JSON Schema generation + committed-file drift ────────────────────────


def test_gen_json_schema_has_version_and_enums():
    s = gen_json_schema()
    assert s["schemaVersion"]
    assert "$id" in s
    props = s["properties"]
    assert props["status"]["enum"] == STATUS_ENUM
    assert props["roi"]["enum"] == ROI_ENUM
    assert props["effort"]["enum"] == EFFORT_ENUM
    capability = props["capability"]["anyOf"][0]
    capability_ref = capability["$ref"].split("/")[-1]
    capability_schema = s["$defs"][capability_ref]
    assert capability_schema["properties"]["class"]["enum"] == list(CAPABILITY_CLASSES)
    assert capability_schema["properties"]["version"]["const"] == (
        CAPABILITY_SCHEMA_VERSION
    )
    assert props["tier"]["deprecated"] is True


def test_committed_schema_matches_generated():
    """docs/_shared/plan.schema.json == gen_json_schema(). Regenerate via
    `python -c 'from reckon._schema import write_json_schema; write_json_schema()'`
    after any model change."""
    p = schema_path()
    assert p.exists(), f"missing {p} — run write_json_schema()"
    committed = json.loads(p.read_text())
    assert committed == gen_json_schema(), (
        "plan.schema.json is stale — regenerate with write_json_schema()"
    )


# ── 8. IndexState modelling of real index.json ──────────────────────────────


def test_index_state_coerces_bare_string_item():
    env = {"data": {"sprints": [{"id": "S0", "items": ["data-acquisition"]}]}}
    st = IndexState.model_validate(env)
    assert st.data.sprints[0].items[0].slug == "data-acquisition"


def test_index_state_coerces_path_keyed_item():
    env = {
        "data": {
            "sprints": [{"id": "S1", "items": [{"path": "plans/x.md", "title": "X"}]}]
        }
    }
    st = IndexState.model_validate(env)
    assert st.data.sprints[0].items[0].slug == "plans/x.md"


def test_index_state_tolerates_null_sprint_summary():
    env = {"data": {"sprints": [{"id": "S2", "summary": None}]}}
    st = IndexState.model_validate(env)
    assert st.data.sprints[0].summary == ""


def test_index_state_preserves_unknown_milestone_fields():
    env = {
        "data": {
            "milestones": [
                {"id": "M0", "name": "x", "status": "shipped", "description": "kept"}
            ]
        }
    }
    st = IndexState.model_validate(env)
    assert (st.data.milestones[0].model_extra or {}).get("description") == "kept"


def test_index_state_version_alias():
    env = {"data": {"_version": 7}}
    st = IndexState.model_validate(env)
    assert st.data.version_ == 7


def test_index_version_dumps_as_underscore_version():
    """A plain model_dump() must emit the on-disk key "_version" (not the
    python field name "version_") — _store.py reads data["_version"], so a
    "version_" key would silently zero the index optimistic-concurrency counter.
    Covers both the IndexData and the IndexState envelope dump paths."""
    data_dump = IndexData.model_validate({"_version": 7}).model_dump()
    assert data_dump["_version"] == 7
    assert "version_" not in data_dump

    env_dump = IndexState.model_validate({"data": {"_version": 7}}).model_dump()
    assert env_dump["data"]["_version"] == 7
    assert "version_" not in env_dump["data"]


def test_index_inventory_excluded_from_write_shape():
    env = {"data": {"inventory": [{"slug": "x"}]}}
    st = IndexState.model_validate(env)
    assert "inventory" not in st.data.model_dump()


# ── 9. Cross-project conformance scan (skip-if-mount-absent) ─────────────────

_INFRA_DIRS = {"_shared", "ui", "state", "assets", "images", "archive"}
_INFRA_STEMS = {
    "index",
    "sprints",
    "sprint",
    "milestones",
    "decisions",
    "inventory",
    "blockers",
    "questions",
    "home",
    "project",
    "implementation",
}


def _mounts_path() -> Path | None:
    """Resolve mounts.json using the environment override and supported homes."""
    env = os.environ.get("RECKON_MOUNTS_PATH")
    if env:
        p = Path(env).expanduser()
        return p if p.exists() else None
    for cand in (
        Path.home() / "docs-server" / "mounts.json",
        Path.home() / ".config" / "reckon" / "mounts.json",
    ):
        if cand.exists():
            return cand
    return None


def _iter_plan_files(docs_dir: Path):
    for f in sorted(docs_dir.rglob("*.html")):
        rel = f.relative_to(docs_dir)
        if any(part in _INFRA_DIRS for part in rel.parts[:-1]):
            continue
        if f.stem in _INFRA_STEMS:
            continue
        yield f


def _all_plan_files():
    mp = _mounts_path()
    if mp is None:
        return None
    try:
        mounts = json.loads(mp.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    files = []
    for project, docs in mounts.items():
        dd = Path(docs).expanduser()
        if not dd.is_dir():
            continue
        for f in _iter_plan_files(dd):
            files.append((project, f))
    return files


_PLAN_FILES = _all_plan_files()


@pytest.mark.skipif(
    not _PLAN_FILES, reason="no mounts.json / mount dirs on this workstation"
)
@pytest.mark.parametrize(
    "project,html_file",
    _PLAN_FILES or [],
    ids=[f"{p}:{f.stem}" for p, f in (_PLAN_FILES or [])],
)
def test_cross_project_conformance(project, html_file):
    """Every existing plan across all mounts must parse via from_html without
    raising. Keep a failing case visible with an explicit migration reason."""
    text = html_file.read_text(encoding="utf-8", errors="replace")
    from_html(text)  # must not raise
