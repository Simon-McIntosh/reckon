"""Typed section metadata survives parsing, regeneration and raw resource reads."""

import json
import re
from copy import deepcopy
from html import escape

import pytest
from jsonschema import Draft202012Validator

from reckon._plan_html import from_html, parse_plan, read_state, write_state
from reckon._schema import PlanState, gen_json_schema

DECLARATIONS = {
    "design": "done",
    "implementation": "implementable",
    "evaluation": "deferred",
}
RECORDS = [
    {
        "id": "design",
        "effort_hours": 0.25,
        "capability": {
            "version": "1.0",
            "class": "general",
            "requirements": {
                "reasoning": "standard",
                "verification": "strict",
                "risk": "low",
            },
        },
        "attempts": 0,
        "status": "done",
        "links": ["inputs", "other:reference#method"],
    },
    {
        "id": "implementation",
        "effort_hours": 2.5,
        "capability": {
            "version": "1.0",
            "class": "orchestrator",
            "requirements": {
                "reasoning": "deep",
                "verification": "strict",
                "risk": "elevated",
            },
        },
        "attempts": 2,
        "status": "implementable",
        "links": ["design#result"],
    },
    {
        "id": "evaluation",
        "effort_hours": 1.0,
        "capability": {
            "version": "1.0",
            "class": "general",
            "requirements": {
                "reasoning": "standard",
                "verification": "standard",
                "risk": "moderate",
            },
        },
        "attempts": 1,
        "status": "deferred",
        "links": [],
    },
]


def _fixture_html(records=None):
    if records is None:
        records = RECORDS
    by_id = {record["id"]: record for record in records}
    body = []
    for identity in DECLARATIONS:
        body.append(f'<h2 id="{identity}">The <em>{identity}</em> work</h2>')
        if identity in by_id:
            record = by_id[identity]
            capability = record["capability"]
            requirements = capability["requirements"]
            body.append(
                f'<section data-reckon="section" data-id="{identity}"'
                f' data-effort-hours="{record["effort_hours"]}"'
                f' data-capability-version="{capability["version"]}"'
                f' data-capability-class="{capability["class"]}"'
                f' data-capability-reasoning="{requirements["reasoning"]}"'
                f' data-capability-verification="{requirements["verification"]}"'
                f' data-capability-risk="{requirements["risk"]}"'
                f' data-attempts="{record["attempts"]}"'
                f' data-status="{record["status"]}"'
                f' data-links="{escape(",".join(record["links"]), quote=True)}"></section>'
            )
        body.append(
            '<p class="authored">Keep &amp; preserve <strong>these bytes</strong>.</p>'
        )
    declarations = escape(json.dumps(DECLARATIONS, separators=(",", ":")), quote=True)
    return (
        "<!doctype html>\n<html><head>\n"
        '<meta name="docs-project" content="sample">\n'
        '<meta name="reckon-type" content="plan">\n'
        '<meta name="plan-slug" content="section-example">\n'
        '<meta name="plan-title" content="Section example">\n'
        f'<meta name="plan-section-declarations" content="{declarations}">\n'
        "</head><body><main>\n" + "\n".join(body) + "\n</main></body></html>\n"
    )


def test_three_sections_parse_all_six_fields(tmp_path):
    path = tmp_path / "section-example.html"
    path.write_text(_fixture_html())
    parsed = parse_plan(path)
    assert parsed["sections"] == RECORDS
    assert all(
        set(record)
        == {"id", "effort_hours", "capability", "attempts", "status", "links"}
        for record in parsed["sections"]
    )
    assert parsed["section_declarations"] == DECLARATIONS
    assert from_html(path.read_text()).canonical_dump()["sections"] == RECORDS


def test_section_record_round_trip_is_byte_stable(tmp_path):
    original = _fixture_html()
    state = read_state(original)
    assert state["sections"] == RECORDS
    rendered = write_state(original, state)
    assert rendered == original
    assert write_state(rendered, from_html(rendered).canonical_dump()) == rendered
    assert read_state(rendered) == state

    # Reconstruct the records from parsed state so leaving stale markup untouched
    # cannot pass as regeneration.
    without_records = re.sub(
        r'<section data-reckon="section"[^>]*></section>\n', "", original
    )
    reconstructed = write_state(without_records, state)
    assert reconstructed == rendered
    path = tmp_path / "section-example.html"
    path.write_text(reconstructed)
    assert parse_plan(path)["sections"] == RECORDS


@pytest.mark.parametrize(
    ("before", "after", "field"),
    [
        ('data-capability-risk="low"', 'data-capability-risk="impossible"', "risk"),
        ('data-effort-hours="0.25"', 'data-effort-hours="0.3"', "effort_hours"),
        ('data-effort-hours="0.25"', 'data-effort-hours="0"', "effort_hours"),
        ('data-effort-hours="0.25"', 'data-effort-hours="nan"', "effort_hours"),
        ('data-attempts="0"', 'data-attempts="-1"', "attempts"),
        ('data-attempts="0"', 'data-attempts="1.5"', "attempts"),
        ('data-attempts="0"', 'data-attempts="true"', "attempts"),
        ('data-status="done"', 'data-status="active"', "status"),
        (
            'data-links="inputs,other:reference#method"',
            'data-links="bad::ref"',
            "links",
        ),
        (
            'data-capability-reasoning="standard"',
            'data-capability-reasoning="unknown"',
            "reasoning",
        ),
        ('data-capability-verification="strict"', "", "verification"),
    ],
)
def test_invalid_section_field_is_refused_at_parse(tmp_path, before, after, field):
    path = tmp_path / "section-example.html"
    path.write_text(_fixture_html().replace(before, after, 1))
    with pytest.raises(ValueError, match=field):
        parse_plan(path)


def test_section_status_must_agree_with_its_declaration(tmp_path):
    path = tmp_path / "section-example.html"
    path.write_text(
        _fixture_html().replace('data-status="done"', 'data-status="deferred"', 1)
    )
    with pytest.raises(ValueError, match=r"status.*declaration"):
        parse_plan(path)


def test_legacy_plan_keeps_declarations_and_has_no_section_records(tmp_path):
    original = _fixture_html([])
    path = tmp_path / "section-example.html"
    path.write_text(original)
    parsed = parse_plan(path)
    assert parsed["sections"] == []
    assert parsed["section_declarations"] == DECLARATIONS
    assert read_state(original)["sections"] == []
    assert write_state(original, read_state(original)) == original


def test_write_updates_record_fields_and_preserves_authored_prose():
    original = _fixture_html()
    state = read_state(original)
    state["sections"][1]["attempts"] = 3
    state["sections"][1]["effort_hours"] = 3.75
    state["sections"][1]["links"] = ["other:inputs#ready"]
    rendered = write_state(original, state)
    assert read_state(rendered)["sections"] == state["sections"]
    assert (
        rendered.count(
            '<p class="authored">Keep &amp; preserve <strong>these bytes</strong>.</p>'
        )
        == 3
    )
    assert '<h2 id="implementation">The <em>implementation</em> work</h2>' in rendered
    assert write_state(rendered, read_state(rendered)) == rendered


def test_published_schema_validates_records_and_refuses_invalid_fields():
    schema = gen_json_schema()
    assert "sections" in schema["properties"]
    validator = Draft202012Validator(schema)
    validator.validate({"sections": RECORDS})
    for field, value in [("effort_hours", 0.3), ("attempts", -1), ("status", "active")]:
        invalid = deepcopy(RECORDS)
        invalid[0][field] = value
        assert list(validator.iter_errors({"sections": invalid}))
    invalid = deepcopy(RECORDS)
    invalid[0]["capability"]["requirements"]["risk"] = "impossible"
    assert list(validator.iter_errors({"sections": invalid}))


def test_raw_read_exposes_section_records(tmp_path, monkeypatch):
    import reckon.mcp as mcp_module

    checkout = tmp_path / "checkout"
    docs = checkout / "docs"
    plans = docs / "plans"
    plans.mkdir(parents=True)
    (plans / "section-example.html").write_text(_fixture_html())
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({"sample": str(docs)}))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(tmp_path / "state"))
    result = mcp_module._read_plan(
        resource={"project": "sample", "type": "plan", "id": "section-example"},
        view="raw",
        checkout_path=str(checkout),
    )
    assert result.get("ok") is not False, result
    assert result["data"]["sections"] == RECORDS


def test_section_records_survive_canonical_typed_dump():
    state = PlanState.model_validate(
        {"sections": RECORDS, "section_declarations": DECLARATIONS}
    )
    assert state.canonical_dump()["sections"] == RECORDS
