"""A review round traces the sections it was asked for, and carries their history.

Work is dispatched one section at a time, and a plan review's history is not
section-scoped: a round composed for three sections reads one brief of prior
findings drawn from the whole plan, so findings about the sections it was asked
about are pushed out by ones about sections it was not. The round then re-traces
code for sections nobody asked about and lacks the history of the ones it was.

A section-scoped round reads the whole plan, traces the code of the sections it
names only, and carries every earlier finding on them in full.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path

import pytest

from reckon import _plan_html
from reckon.crew import plan_review, recovery, review
from tests.test_review_plan_command import project as project  # noqa: PLC0414

SECTIONS = ("s1", "s2", "s3", "s4", "s5", "s6")

PROSE = {
    "s1": "Gate every node on its own section.",
    "s2": "Read the change the edit made.",
    "s3": "Trace the resolver the section cites.",
    "s4": "Carry the history of the round.",
    "s5": "Name the owner the section reuses.",
    "s6": "Measure the cost of the read.",
}

PLAN = "docs/plans/fixture.html"
CONTENT_VERSION = 5

# The findings the fixture's history carries: twenty-five on §3 and one on each
# other section, which is the shape a plan with one much-edited section takes.
S3_FINDINGS = 25

# One finding long enough that any per-finding character bound cuts it, so a
# brief that carries it whole is carrying the history whole.
LONG_S3_TEXT = "§3 finding 0: " + (
    "the section cites a resolver the plan never names, and the reader is left "
    "to derive which owner answers the citation " * 8
)


def _report(items) -> str:
    return "".join(f"RUBRIC {item}: pass\n" for item in items)


@pytest.fixture
def sectioned_plan(project):  # noqa: F811
    """A plan of six implementable sections sharing one introduction."""
    _home, _repo, path = project
    body = "".join(
        f'<h2 id="{name}">Section {name}</h2><p>{PROSE[name]}</p>' for name in SECTIONS
    )
    text = path.read_text().replace(
        '<h2 id="delivery">Delivery</h2><p>Extend the existing mechanism.</p>',
        f"<p>Shared introduction.</p>{body}",
    )
    state = _plan_html.read_state(text)
    state["section_declarations"] = dict.fromkeys(SECTIONS, "implementable")
    state["sections"] = [
        {
            "id": name,
            "effort_hours": 1.0,
            "status": "implementable",
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
            "links": [],
        }
        for name in SECTIONS
    ]
    path.write_text(_plan_html.write_state(text, state))
    return path


def _subject(path, run_id: str, rubric: str) -> dict:
    return {
        "subject": "plan",
        "project": "sample",
        "plan_slug": "fixture",
        "plan_path": str(path),
        "repo": str(Path(path).parents[2]),
        "session": "coordinator",
        "rubric": rubric,
        "local": True,
        "run_id": run_id,
    }


def _brief(path, *, sections=None, rubric: str = "content") -> str:
    """The brief a review composed now would carry, written nowhere."""
    subject = _subject(path, f"review-{sections or 'whole'}", rubric)
    scope = (
        plan_review.reviewing_sections(sections)
        if sections is not None
        else nullcontext()
    )
    with scope:
        return recovery._review_dispatch_fields(subject, write=False, rubric=rubric)[
            "brief_text"
        ]


def _findings() -> list[dict]:
    findings = []
    for index in range(S3_FINDINGS):
        text = (
            "§3 finding "
            f"{index}: the section cites a resolver the plan never names, and the "
            "reader is left to derive which owner answers the citation"
        )
        if index == 0:
            text = LONG_S3_TEXT
        findings.append(
            {
                "id": f"finding-s3-{index}",
                "type": "reuse_search",
                "anchor": f"{PLAN}#s3",
                "text": text,
            }
        )
    for name in SECTIONS:
        if name == "s3":
            continue
        findings.append(
            {
                "id": f"finding-{name}-0",
                "type": "reasoning",
                "anchor": f"{PLAN}#{name}",
                "text": f"§{name[1:]} finding: the section's premise is unchecked.",
            }
        )
    return findings


def _responses() -> dict:
    return {
        f"finding-s3-{index}": {
            "action": "acted",
            "note": f"finding {index} fixed: the resolver is named in §3.",
        }
        for index in range(S3_FINDINGS)
    }


def _store(
    path,
    *,
    run_id: str = "review-content",
    sections=None,
    findings=None,
    responses=None,
    version: int = CONTENT_VERSION,
) -> dict:
    """Store one complete content review of the plan as it stands now."""
    subject = _subject(path, run_id, "content")
    scope = (
        plan_review.reviewing_sections(sections)
        if sections is not None
        else nullcontext()
    )
    with scope:
        fields = recovery._review_dispatch_fields(subject, rubric="content")
    sidecar = json.loads(Path(fields["sidecar"]).read_text())
    sidecar["plan_version"] = version
    Path(sidecar["report_path"]).write_text(_report(review.PLAN_REVIEW_ITEMS))
    record = {
        **sidecar,
        "findings": _findings() if findings is None else list(findings),
        "responses": _responses() if responses is None else dict(responses),
        "review_run_id": run_id,
    }
    plan_review.store_plan_review(record)
    return record


def test_a_scoped_review_carries_every_finding_on_its_section(sectioned_plan):
    """All 25 findings on §3, each with its answer and note, and none of the rest."""
    _store(sectioned_plan, sections=["s3"])

    brief = _brief(sectioned_plan, sections=["s3"])
    scope = brief[brief.index("Scope:") :]

    for index in range(S3_FINDINGS):
        assert f"finding-s3-{index}" in scope
    for name in ("s1", "s2", "s4", "s5", "s6"):
        assert f"finding-{name}-0" not in scope
    # Every one carries its answer and the note the answer was given with.
    assert scope.count("-> acted: ") == S3_FINDINGS
    assert "finding 7 fixed: the resolver is named in §3." in scope
    # In full: the longest finding is not cut and no elision marks it.
    assert " ".join(LONG_S3_TEXT.split()) in scope
    assert "…" not in scope


def test_a_scoped_brief_carries_the_whole_plan_and_names_the_only_unit_traced(
    sectioned_plan,
):
    _store(sectioned_plan, sections=["s3"])

    brief = _brief(sectioned_plan, sections=["s3"])

    # The whole plan each round, so a scoped review reads the other sections for
    # consistency with the one it traces.
    for name in SECTIONS:
        assert PROSE[name] in brief
    assert "The whole plan this review reads" in brief
    # And one unit is named as the one whose code is traced.
    assert "The units whose code this review traces, and the only ones: s3." in brief


def test_a_first_review_composed_with_a_section_widens_to_every_unit(
    sectioned_plan,
):
    """Nothing is stored, so no section can be known settled and all are in scope."""
    brief = _brief(sectioned_plan, sections=["s3"])

    scope = brief[brief.index("Scope:") :]
    assert "widens to the whole plan" in scope
    for name in SECTIONS:
        assert name in scope.split("\n")[0]
    # The plan's text is carried, as for any round.
    assert PROSE["s5"] in brief


def test_a_widened_first_review_records_the_units_it_actually_traced(sectioned_plan):
    """The record keeps what the round traced, not what it was asked about.

    A first round asked for §3 widens to every unit, and the round that reads
    them all must cover them all: a record narrowed to the asking would leave
    the sections the brief widened past uncovered, so the one full round a plan
    ever gets would cover a sixth of it.
    """
    _store(sectioned_plan, sections=["s3"])

    record = plan_review.read_plan_review("sample", "fixture", plan=sectioned_plan)
    assert record is not None
    assert set(record[plan_review.TRACED_UNITS_KEY]) == set(SECTIONS) | {
        plan_review.DOCUMENT_UNIT
    }

    _records, uncovered, _changes = plan_review.review_coverage(
        "sample", "fixture", plan=sectioned_plan
    )
    assert sorted(uncovered) == []


def test_the_stored_scope_covers_the_section_it_traced_and_no_other(sectioned_plan):
    # A first round reads the whole plan, so a scope that narrows is a later
    # round; and §5 is rewritten after that round, so only a round that traces
    # it can cover it.
    _store(sectioned_plan, run_id="review-first")
    sectioned_plan.write_text(
        sectioned_plan.read_text().replace(
            PROSE["s5"], "Name the owner the section reuses, not the one it cites."
        )
    )
    _store(sectioned_plan, run_id="review-second", sections=["s3"])
    record = plan_review.read_plan_review("sample", "fixture", plan=sectioned_plan)
    assert record is not None
    assert record[plan_review.TRACED_UNITS_KEY] == ["s3"]

    _records, uncovered, _changes = plan_review.review_coverage(
        "sample", "fixture", plan=sectioned_plan
    )

    # §3 was traced and its text has not moved, so the review covers it.
    assert "s3" not in uncovered
    # A section the review read only for consistency stays uncovered.
    assert "s5" in uncovered


def test_a_scoped_review_and_a_whole_plan_review_carry_the_same_findings_for_a_section(
    sectioned_plan,
):
    _store(sectioned_plan, sections=["s3"])

    scoped = _brief(sectioned_plan, sections=["s3"])
    whole = _brief(sectioned_plan, sections=list(SECTIONS))

    def on_s3(text: str) -> list[str]:
        return [line for line in text.splitlines() if line.startswith("- finding-s3-")]

    assert on_s3(scoped)
    assert on_s3(whole) == on_s3(scoped)


def test_the_section_option_scopes_a_plan_review(sectioned_plan):
    """The command carries the scope, and refuses it where it does not apply."""
    from click.testing import CliRunner

    from reckon import cli

    runner = CliRunner()
    misapplied = runner.invoke(
        cli.main,
        [
            "crew",
            "review",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--section",
            "s3",
            "--answer",
            "f1",
            "--acted",
            "--note",
            "the fix landed",
            "--session",
            "coordinator",
        ],
    )
    assert misapplied.exit_code == 1
    assert "--section scopes a plan content or design review" in misapplied.output

    preview = runner.invoke(
        cli.main,
        [
            "crew",
            "review",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--rubric",
            "content",
            "--section",
            "s3",
            "--dry-run",
            "--session",
            "coordinator",
            "--pretty",
        ],
    )
    assert preview.exit_code == 0, preview.output
    assert "widens to the whole plan" in preview.output
