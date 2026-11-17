"""An edit recorded as the answer to a finding keeps its section covered.

A coordinator answers a plan-review finding by editing the plan and recording
the answer. The edit is the review's own subject, so the section it changed is
covered by that review while the section still holds the text the answer was
recorded against; a later edit moves that text and the section owes a review
again. A finding answered ``declined`` asks for no edit at all, so a section
edited beside a decline is still uncovered.

The cases drive admission passes over a synthesised repository and crew home
under ``RECKON_HOME``: two sections edited and answered ``acted`` leave both
requests launchable with no review composed, a later edit to one section holds
that section's request alone, and a decline leaves its section held.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import flight
from reckon.crew import obligations as obligations_module
from reckon.crew import plan_review, queue_admission, recovery, runs
from reckon.crew.node import TaskNode

PROJECT = "proj"
SESSION = "coordinator-a"
PLAN = "plan-a"

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
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}, "review": {"backend": "clive"}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    "plan_review_gate": "enforce",
}

DONE_WHEN = {
    "s2": "bravo ships and nothing regresses",
    "s3": "charlie ships and nothing regresses",
}


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=repository, check=True, capture_output=True
    )
    return result.stdout.decode().strip()


def _words(prefix: str, count: int = 12) -> str:
    return " ".join(f"{prefix}{index:02d}word" for index in range(count))


def _section_html(name: str, done_when: str) -> str:
    heading = f'<h2 id="{name}">{name}</h2>'
    return (
        heading
        + f"<p>{_words(name)}</p>"
        + f"<p><strong>Done when</strong>: {done_when}.</p>"
    )


def _plan_html(declarations, done_when) -> str:
    sections = "".join(
        _section_html(name, done_when.get(name, f"{name} ships"))
        for name in declarations
    )
    declared = json.dumps(declarations).replace('"', "&quot;")
    return (
        "<!doctype html>\n<html><head>\n"
        f'<meta name="docs-project" content="{PROJECT}">\n'
        '<meta name="reckon-type" content="plan">\n'
        f'<meta name="plan-slug" content="{PLAN}">\n'
        '<meta name="plan-title" content="Plan A">\n'
        '<meta name="plan-status" content="active">\n'
        '<meta name="plan-version" content="3">\n'
        f'<meta name="plan-section-declarations" content="{declared}">\n'
        "</head><body>\n"
        f"{sections}"
        "</body></html>\n"
    )


def _write_plan(repository: Path, done_when=None) -> None:
    declarations = {"s2": "implementable", "s3": "implementable"}
    (repository / "docs" / "plans" / f"{PLAN}.html").write_text(
        _plan_html(declarations, done_when or DONE_WHEN), encoding="utf-8"
    )


def _commit(repository: Path, message: str) -> str:
    _git(repository, "add", "--", "docs")
    _git(repository, "commit", "-q", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(config_home / "cache" / "velocity"))
    monkeypatch.setattr(
        flight, "resolve", lambda *args, **kwargs: SimpleNamespace(config=CONFIG)
    )

    repository = tmp_path / "checkout"
    (repository / "docs" / "plans").mkdir(parents=True)
    _write_plan(repository)
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    _commit(repository, "chore: seed the plan")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _store_review(path: Path, *, run_id: str, findings=()) -> dict:
    """Store a review of the plan as it stands, with its snapshot to measure."""
    subject = {
        "subject": "plan",
        "project": PROJECT,
        "plan_slug": PLAN,
        "plan_path": str(path),
        "repo": str(path.parents[2]),
        "session": SESSION,
        "rubric": "design",
        "local": True,
        "run_id": run_id,
    }
    fields = recovery._review_dispatch_fields(subject)
    sidecard = json.loads(Path(fields["sidecar"]).read_text(encoding="utf-8"))
    Path(sidecard["report_path"]).write_text("RUBRIC wiring: pass\n", encoding="utf-8")
    record = {
        **sidecard,
        "findings": list(findings),
        "responses": {},
        "review_run_id": run_id,
    }
    plan_review.store_plan_review(record)
    return record


def _finding(section: str) -> dict:
    return {
        "id": f"fix-{section}",
        "anchor": f"docs/plans/{PLAN}.html#{section}",
        "type": "correctness",
        "text": f"Section {section}'s done-when does not say what ships.",
    }


def _answer(finding_id: str, action: str, *, reason: str = "", note: str = ""):
    record = plan_review.read_plan_review(PROJECT, PLAN)
    assert record is not None, "the fixture stores a review before answering one"
    return plan_review.record_response(
        record,
        finding_id,
        action=action,
        reason=reason or None,
        note=note,
        by="coordinator",
    )


def _edit_done_when(path: Path, section: str, replacement: str) -> None:
    """Replace the section's done-when sentence, whatever it says now."""
    text = path.read_text(encoding="utf-8")
    heading = text.index(f'<h2 id="{section}">')
    marker = "<p><strong>Done when</strong>: "
    start = text.index(marker, heading) + len(marker)
    end = text.index(".</p>", start)
    path.write_text(text[:start] + replacement + text[end:], encoding="utf-8")


def _node(node_id: str, section: str) -> TaskNode:
    return TaskNode(
        id=node_id,
        goal=f"build {section}",
        plan=PLAN,
        section=section,
        done_when=f"pytest reports passed for {section}",
        write_paths=[f"{section}.txt"],
        time_budget="20m",
        spec_level="exact",
    )


def _queue(repository: Path, entries):
    nodes = []
    ids: dict[str, str] = {}
    for run_id, section in entries:
        node_id = run_id.replace("r-", "n-")
        nodes.append(_node(node_id, section))
        ids[node_id] = run_id
    return queue_admission.queue_requests(
        nodes,
        project=PROJECT,
        session=SESSION,
        repo_root=repository,
        config=CONFIG,
        run_ids=ids,
    )


def _config_for():
    def resolve(pointer):
        return CONFIG

    return resolve


def _launcher(recorded: list[str]):
    def launch(plan, *, log_path, stderr_path, prompt_path):
        recorded.append(Path(str(prompt_path)).name)
        return os.getpid()

    return launch


def _pointer(run_id: str) -> dict:
    return runs.read_pointer(run_id)


def _admit(launched: list[str]) -> dict:
    return queue_admission.admit_once(
        config_for=_config_for(), launcher=_launcher(launched)
    )


def _review_obligations() -> list[dict]:
    view = obligations_module.obligations(PROJECT, SESSION)
    return [row for row in view["obligations"] if row.get("kind") == "plan-review-owed"]


def test_answering_edits_leave_both_sections_covered(project: Path) -> None:
    """Both edited-and-answered sections launch, and no review is composed."""
    path = project / "docs" / "plans" / f"{PLAN}.html"
    _store_review(path, run_id="review-one", findings=[_finding("s2"), _finding("s3")])
    _queue(project, [("r-s2", "s2"), ("r-s3", "s3")])
    assert _pointer("r-s2")["phase"] == queue_admission.QUEUED_PHASE
    assert _pointer("r-s3")["phase"] == queue_admission.QUEUED_PHASE

    _edit_done_when(path, "s2", "bravo ships and its acceptance criterion holds")
    _edit_done_when(path, "s3", "charlie ships and its acceptance criterion holds")
    _commit(project, "docs: answer the two findings")
    _answer("fix-s2", "acted", note="The done-when now names what ships.")
    _answer("fix-s3", "acted", note="The done-when now names what ships.")

    stored = plan_review.read_plan_review(PROJECT, PLAN)
    answer = stored["responses"]["fix-s2"]
    assert answer.get("commit_basis") == plan_review.EDIT_COMMIT, answer
    assert answer.get("commit"), answer

    launched: list[str] = []
    result = _admit(launched)

    assert len(launched) == 2, (
        f"both answered sections launch: {result} launched {launched}"
    )
    assert not result["held"], "no request is held for a review"
    assert not _review_obligations(), "no review is composed for the plan"


def test_a_later_edit_holds_only_its_own_section(project: Path) -> None:
    """After the answers, an unanswered done-when edit holds its section alone."""
    path = project / "docs" / "plans" / f"{PLAN}.html"
    _store_review(path, run_id="review-one", findings=[_finding("s2"), _finding("s3")])
    _queue(project, [("r-s2", "s2"), ("r-s3", "s3")])
    _edit_done_when(path, "s2", "bravo ships and its acceptance criterion holds")
    _edit_done_when(path, "s3", "charlie ships and its acceptance criterion holds")
    _commit(project, "docs: answer the two findings")
    _answer("fix-s2", "acted", note="The done-when now names what ships.")
    _answer("fix-s3", "acted", note="The done-when now names what ships.")
    launched: list[str] = []
    _admit(launched)
    assert len(launched) == 2, "the answered edits are covered before the next edit"

    _queue(project, [("r2-s2", "s2"), ("r2-s3", "s3")])
    _edit_done_when(path, "s2", "bravo ships and nothing else moves")
    _commit(project, "docs: steer section two")

    launched.clear()
    result = _admit(launched)

    assert len(launched) == 1, f"only the untouched section launches: {result}"
    held = _pointer("r2-s2")
    assert held["phase"] == queue_admission.HELD_PHASE, (result, held)
    assert held["queue_hold"]["reason"] == queue_admission.PLAN_REVIEW_HELD_REASON
    assert "uncovered units: s2" in held["queue_hold"]["detail"], held["queue_hold"]
    assert _pointer("r2-s3")["phase"] != queue_admission.HELD_PHASE


def test_a_decline_beside_an_edit_leaves_the_section_uncovered(project: Path) -> None:
    """A declined finding asks for no edit, so the edit beside it owes a review."""
    path = project / "docs" / "plans" / f"{PLAN}.html"
    _store_review(path, run_id="review-one", findings=[_finding("s3")])
    _queue(project, [("r-s3", "s3")])
    _edit_done_when(path, "s3", "charlie ships and its acceptance criterion holds")
    _commit(project, "docs: rewrite section three")
    _answer("fix-s3", "declined", reason="The criterion is the reviewer's to change.")

    launched: list[str] = []
    _admit(launched)

    assert not launched, "a declined finding asks for no edit and covers none"
    held = _pointer("r-s3")
    assert held["phase"] == queue_admission.HELD_PHASE
    assert "uncovered units: s3" in held["queue_hold"]["detail"], held["queue_hold"]
