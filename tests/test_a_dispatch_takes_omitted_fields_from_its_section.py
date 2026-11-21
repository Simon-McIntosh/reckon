"""A dispatch takes every field it omits from the section it names.

A node that names a plan section names its authority, and the section already
states the measure, the sizing, the specification level and the mutation its
check must fail against. Asking a coordinator to repeat them is what refused a
section-only batch entry for "no time budget is set; no specification level is
declared", and left a single dispatch without the section's done-when.

The second half is the placeholder rule. A bracketed token is a placeholder only
when it stands alone: one embedded in a filename pattern, or an HTML element
written as test data, is a literal the worker reproduces rather than a gap only
the coordinator can close.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import dispatch_batch, dispatch_plan

PROJECT = "proj"
SESSION = "coordinator-a"
PLAN = "plan-a"

CONFIG = {
    "default_backend": "worker",
    "backends": {
        "worker": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "8h", "needs_help_after_failures": 2},
}

#: The section's own values. Every assertion compares a stored field against
#: one of these, so a derivation that happens to agree with the backend default
#: cannot pass for the section having supplied it.
EFFORT_HOURS = 2.5
SECTION_BUDGET = "150m"
SECTION_SPEC_LEVEL = "guided"
NEGATIVE_CONTROL = (
    "the section-only entry against the base revision is refused, naming the "
    "budget and specification level"
)

DONE_WHEN = (
    "<p><strong>Done when.</strong> Tests with a temporary repository show "
    "the following:</p>"
    "<ul><li>A done-when naming <code>pf&lt;n&gt;-terminal-edge.json</code> is "
    "accepted.</li>"
    "<li>The tag <code>&lt;strong&gt;</code> inside a code span is "
    "accepted.</li></ul>"
    f"<p>Negative control: {NEGATIVE_CONTROL}.</p>"
)


def _plan(slug: str, prose: str) -> str:
    quote = '"'
    declarations = json.dumps({"s1": "implementable"}).replace(quote, "&quot;")
    return (
        "<!doctype html>\n"
        f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>\n"
        f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>\n"
        f"<meta name={quote}plan-slug{quote} content={quote}{slug}{quote}>\n"
        f"<meta name={quote}plan-status{quote} content={quote}active{quote}>\n"
        f"<meta name={quote}plan-section-declarations{quote} content={quote}"
        f"{declarations}{quote}>\n"
        f'<h2 id="s1">§1</h2>'
        f'<section data-reckon="section" data-id="s1" '
        f'data-effort-hours="{EFFORT_HOURS}" data-capability-version="1.0" '
        f'data-capability-class="general" data-capability-reasoning="deep" '
        f'data-capability-tool-autonomy="guided" '
        f'data-capability-verification="strict" data-capability-risk="moderate" '
        f'data-status="implementable"></section>'
        f"{prose}\n"
    )


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments], cwd=repository, check=True, capture_output=True
    )


@pytest.fixture()
def checkout(tmp_path: Path, home: Path) -> Path:
    """A git checkout carrying one plan, under a temporary crew home."""
    repository = tmp_path / "checkout"
    state = repository / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    plans = repository / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{PLAN}.html").write_text(_plan(PLAN, DONE_WHEN), encoding="utf-8")
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    _git(repository, "add", "--", "docs")
    _git(repository, "commit", "-q", "-m", "chore: seed checkout")
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _section_only_entry() -> dict:
    """An entry naming only its project, plan, section, node and goal."""
    return {
        "project": PROJECT,
        "plan": PLAN,
        "section": "s1",
        "node": "section-only",
        "goal": "run the section with nowhere else to read the fields from",
    }


def _stored_node(node_id: str) -> dict:
    from reckon.crew.runs import node_definition

    for pointer in crew.list_live(project=PROJECT):
        node = pointer.get("node") or {}
        if str(node.get("id") or "") == node_id:
            # The body fields live in the run's node file, so a reader that
            # opened only the pointer would find none of the derived fields.
            return node_definition(pointer)
    raise AssertionError(f"no queued request for node {node_id}")


def _queue(repository: Path, entries: list[dict]) -> dict:
    def config_for(_project: str) -> dict:
        return json.loads(json.dumps(CONFIG))

    return dispatch_batch.queue_batch(
        entries,
        session=SESSION,
        config_for=config_for,
        repo_root=repository,
    )


def test_a_section_only_batch_entry_takes_every_field_from_its_section(
    checkout: Path,
) -> None:
    result = _queue(checkout, [_section_only_entry()])
    assert result["ok"] is True, json.dumps(result["results"], indent=2)
    assert result["queued"] == 1

    stored = _stored_node("section-only")
    assert stored["done_when"].startswith("Done when.")
    assert "pf<n>-terminal-edge.json" in stored["done_when"]
    assert "<strong>" in stored["done_when"]
    assert stored["time_budget"] == SECTION_BUDGET
    assert stored["time_budget"] != CONFIG["backends"]["worker"]["time_budget"]
    assert stored["spec_level"] == SECTION_SPEC_LEVEL
    assert stored["negative_control"] == NEGATIVE_CONTROL
    # The section's own values are recorded as the ones it supplied, so a
    # release re-derives the same four fields rather than the entry's.
    assert set(stored["section_derived"]) >= {
        "done_when",
        "time_budget",
        "spec_level",
        "negative_control",
    }


def test_a_single_dispatch_that_omits_the_done_when_takes_the_sections(
    checkout: Path,
) -> None:
    node = crew.TaskNode(
        id="single-dispatch",
        goal="implement the section without repeating what it states",
        plan=crew.normalize_section(PLAN),
        section="s1",
        write_paths=[],
    )

    plan = dispatch_plan.plan_dispatch(
        node=node,
        config=CONFIG,
        project=PROJECT,
        repo=checkout,
        route="deterministic",
        allow_unreviewed_plan=True,
    )

    assert plan.validation.ok, plan.validation.findings
    assert node.done_when.startswith("Done when.")
    assert "pf<n>-terminal-edge.json" in node.done_when
    assert node.time_budget == SECTION_BUDGET
    assert node.spec_level == SECTION_SPEC_LEVEL
    assert node.negative_control == NEGATIVE_CONTROL


def _verdict(done_when: str):
    return crew.validate_node(
        crew.TaskNode(
            id="placeholder",
            goal="write the pattern the section names",
            plan=PLAN,
            section="s1",
            spec_level="exact",
            done_when=done_when,
            time_budget="20m",
            manifest_path="/tmp/manifest.md",
            negative_control="none: a pattern is data, not a check",
        )
    )


def test_a_code_span_naming_a_filename_pattern_is_accepted() -> None:
    """The token stands beside a file extension, so it is a literal."""
    verdict = _verdict(
        "Tests show `pf<n>-terminal-edge.json` is written, and "
        "`tests/<name>.py` is collected, and pytest passes."
    )
    assert verdict.ok, verdict.findings


def test_an_html_tag_written_as_test_data_is_accepted() -> None:
    """A bracketed tag is markup to reproduce, not a value to fill."""
    verdict = _verdict(
        "Tests show `<strong>` is preserved in the rendered plan and pytest passes."
    )
    assert verdict.ok, verdict.findings


def test_a_bare_bracketed_token_is_still_refused() -> None:
    """A token standing alone is a gap, in prose and in a code span alike."""
    bare = _verdict("Tests show the census for <n> is complete and pytest passes.")
    assert not bare.ok
    assert any(
        finding["property"] == "fully-specified" and "<n>" in finding["detail"]
        for finding in bare.findings
    )

    code_span = _verdict(
        "Tests show `<project>` carries the census and pytest passes."
    )
    assert not code_span.ok
    assert any(
        "<project>" in finding["detail"] for finding in code_span.findings
    )