"""A section's write-paths sentence is read whatever punctuation follows the label.

A plan section states the paths a node may write in one bold lead-in, and it
authors that lead-in either ``Write paths:`` or ``Write paths.``. Both readers
of the sentence — the section reader that composes a code review and the batch
entry reader that derives a node's fence — must take the paths from either
form, or a plan that names its paths with a full stop has them refused
downstream as if it named none. ``reckon audit-doc`` reports a section whose
sentence is in a form neither reader parses, so the label's shape is visible
before a dispatch is refused for it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import doccheck
from reckon.crew import dispatch_batch, runs
from reckon.crew.plan_review import code_review_paths_from_plan

PROJECT = "proj"
SESSION = "coordinator-a"
PLAN = "plan-a"
MODULE = "reckon/crew/x.py"
FULL_STOP = f"<strong>Write paths.</strong> <code>{MODULE}</code>"
COLON = f"<strong>Write paths:</strong> <code>{MODULE}</code>"
UNPARSEABLE = f"<strong>Write paths</strong> <code>{MODULE}</code>"
DONE_WHEN = (
    "pytest -q tests/test_a_write_paths_sentence_is_read_in_either_form.py passes."
)

CONFIG = {
    "default_backend": "clive",
    "local_backend": "clive",
    "backends": {
        "clive": {
            "launch": "cli",
            "command": "codex",
            "model": "local-model",
            "effort": "medium",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {"implement": {}},
    "fences": {
        "time_budget": "2h",
        "needs_help_after_failures": 2,
    },
}

SECTION_ATTRS = (
    'data-reckon="section" data-id="s1" data-effort-hours="0.5" '
    'data-capability-version="1.0" data-capability-class="general" '
    'data-capability-reasoning="standard" '
    'data-capability-verification="strict" data-capability-risk="moderate" '
    'data-status="implementable"'
)


def _plan_document(sentence: str) -> str:
    quote = '"'
    declarations = json.dumps({"s1": "implementable"}).replace(quote, "&quot;")
    return (
        "<!doctype html>\n"
        f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>\n"
        f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>\n"
        f"<meta name={quote}plan-slug{quote} content={quote}{PLAN}{quote}>\n"
        f"<meta name={quote}plan-status{quote} content={quote}active{quote}>\n"
        f"<meta name={quote}plan-section-declarations{quote} "
        f"content={quote}{declarations}{quote}>\n"
        f"<h2 id={quote}s1{quote}>§1 — one section</h2>"
        f"<section {SECTION_ATTRS}></section>"
        f"<p>{sentence}.</p>"
        f"<p><strong>Done when</strong> {DONE_WHEN}</p>\n"
    )


def _write_plan(repository: Path, sentence: str) -> Path:
    plan = repository / "docs" / "plans" / f"{PLAN}.html"
    plan.write_text(_plan_document(sentence), encoding="utf-8")
    return plan


def _run(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


@pytest.fixture()
def batch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repository = tmp_path / "checkout"
    state = repository / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    plans = repository / "docs" / "plans"
    plans.mkdir(parents=True)
    _write_plan(repository, FULL_STOP)
    (repository / "reckon" / "crew").mkdir(parents=True)
    (repository / MODULE).write_text("", encoding="utf-8")
    _run(repository, "init", "-q", "-b", "main")
    _run(repository, "config", "user.email", "worker@example.invalid")
    _run(repository, "config", "user.name", "Worker")
    _run(repository, "add", "--", "docs", "reckon")
    _run(repository, "commit", "-q", "-m", "chore: seed checkout")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _entry(section: str) -> dict:
    return {
        "project": PROJECT,
        "plan": PLAN,
        "section": section,
        "node": f"node-{section}",
        "goal": "Write the module the section names.",
        "time_budget": "20m",
        "spec_level": "exact",
    }


def _queue(repository: Path, section: str) -> dict:
    return dispatch_batch.queue_batch(
        [_entry(section)],
        session=SESSION,
        config_for=lambda _project: json.loads(json.dumps(CONFIG)),
        repo_root=repository,
    )


def test_a_full_stop_sentence_yields_its_path_to_the_section_reader(
    batch_repo: Path,
) -> None:
    plan = _write_plan(batch_repo, FULL_STOP)

    assert MODULE in code_review_paths_from_plan(plan)


def test_a_full_stop_sentence_yields_its_path_to_the_batch_reader(
    batch_repo: Path,
) -> None:
    _write_plan(batch_repo, FULL_STOP)
    read = dispatch_batch._PlanRead(PROJECT, PLAN, batch_repo)

    assert read.write_paths("s1") == [MODULE]


def test_the_colon_form_still_yields_its_paths(batch_repo: Path) -> None:
    plan = _write_plan(batch_repo, COLON)
    read = dispatch_batch._PlanRead(PROJECT, PLAN, batch_repo)

    assert MODULE in code_review_paths_from_plan(plan)
    assert read.write_paths("s1") == [MODULE]


def test_a_batch_entry_for_a_full_stop_section_queues_with_its_paths(
    batch_repo: Path,
) -> None:
    result = _queue(batch_repo, "s1")

    assert result["queued"] == 1, result
    row = result["results"][0]
    pointer = runs.read_pointer(row["run_id"])
    assert pointer["node"]["write_paths"] == [MODULE]


def test_audit_doc_warns_on_an_unparseable_write_paths_sentence() -> None:
    findings = doccheck.audit_html(_plan_document(UNPARSEABLE))

    assert any(
        finding.code == "write-paths-sentence-unparsed" for finding in findings
    ), [f.fmt() for f in findings]


@pytest.mark.parametrize("sentence", [FULL_STOP, COLON])
def test_audit_doc_accepts_both_parseable_forms(sentence: str) -> None:
    findings = doccheck.audit_html(_plan_document(sentence))

    assert not any(
        finding.code == "write-paths-sentence-unparsed" for finding in findings
    ), [f.fmt() for f in findings]


def test_a_prose_mention_beside_a_parseable_label_raises_no_warning() -> None:
    mention = "<p>The write paths below are the fence for this section.</p>"
    document = _plan_document(COLON).replace(
        f"<p>{COLON}.</p>", f"{mention}<p>{COLON}.</p>"
    )

    findings = doccheck.audit_html(document)

    assert not any(
        finding.code == "write-paths-sentence-unparsed" for finding in findings
    ), [f.fmt() for f in findings]
