"""A plan edit never strands a queued request.

A coordinator steers queued work by editing the plan, so an edit must reach the
request it is about and nothing else. Two things follow, and both are measured
here.

At admission the plan-review gate is asked about the units a request reads —
its own section and the document unit carrying decisions, gates and
dependencies — rather than about the whole plan. An edit to another section
then lets this request launch, and a request whose own section was edited moves
to ``held`` with the reason ``plan-review-owed`` and the invocation that
composes the review it owes. Storing that review releases it on the next
admission pass, with no coordinator call, while a request a coordinator held by
hand stays held.

The plan's waits reach it the same way. Every pass re-derives a waiting or
queued request's waits from the plan it was dispatched from, keeping the
request's own ``--after`` refs, so a narrowed wait releases the request and a
widened one sends it back to ``awaiting-upstream``.

The fixture is a synthesised mounted project under ``RECKON_HOME``, so nothing
here reads or writes the operator's crew home.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import flight
from reckon.crew import (
    obligations as obligations_module,
)
from reckon.crew import (
    plan_review,
    queue_admission,
    recovery,
    runs,
)
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
    # The gate is enforced here because that is the mode in which the defect was
    # measured: under the shipped report-only default the launch proceeds and
    # the strand never happens.
    "plan_review_gate": "enforce",
}

DONE_WHEN = {
    "s2": "bravo ships and nothing regresses",
    "s3": "charlie ships and nothing regresses",
    "s4": "delta ships and nothing regresses",
    "s5": "echo ships and nothing regresses",
}


def _run(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


def _words(prefix: str, count: int = 12) -> str:
    return " ".join(f"{prefix}{index:02d}" for index in range(count))


def _section_html(name: str, done_when: str) -> str:
    return (
        f'<h2 id="{name}">§{name[1:]}</h2>'
        f"<p>{_words(name)}</p>"
        f"<p><strong>Done when</strong>: {done_when}.</p>"
    )


def _plan_html(
    declarations: dict[str, str],
    waits: dict[str, list[str]] | None = None,
    done_when: dict[str, str] | None = None,
) -> str:
    done_when = done_when or DONE_WHEN
    sections = "".join(_section_html_section(name, done_when) for name in declarations)
    declarations_meta = json.dumps(declarations).replace('"', "&quot;")
    waits_meta = ""
    if waits is not None:
        declared = json.dumps(waits).replace('"', "&quot;")
        waits_meta = f'<meta name="plan-section-depends-on" content="{declared}">\n'
    return (
        "<!doctype html>\n<html><head>\n"
        f'<meta name="docs-project" content="{PROJECT}">\n'
        '<meta name="reckon-type" content="plan">\n'
        f'<meta name="plan-slug" content="{PLAN}">\n'
        '<meta name="plan-title" content="Plan A">\n'
        '<meta name="plan-status" content="active">\n'
        '<meta name="plan-version" content="3">\n'
        f'<meta name="plan-section-declarations" content="{declarations_meta}">\n'
        f"{waits_meta}"
        "</head><body>\n"
        f"{sections}"
        "</body></html>\n"
    )


def _section_html_section(name: str, done_when: dict[str, str]) -> str:
    return _section_html(name, done_when.get(name, f"{name} ships"))


def _write_plan(
    repository: Path, declarations: dict[str, str], waits=None, done_when=None
) -> None:
    (repository / "docs" / "plans" / f"{PLAN}.html").write_text(
        _plan_html(declarations, waits, done_when), encoding="utf-8"
    )


def _commit(repository: Path, message: str) -> None:
    _run(repository, "add", "--", "docs")
    _run(repository, "commit", "-q", "-m", message)


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(config_home / "cache" / "velocity"))
    monkeypatch.setattr(
        flight, "resolve", lambda *args, **kwargs: SimpleNamespace(config=CONFIG)
    )

    repository = tmp_path / "checkout"
    (repository / "docs" / "plans").mkdir(parents=True)
    _write_plan(
        repository,
        {
            "s2": "implementable",
            "s3": "implementable",
            "s4": "implementable",
            "s5": "implementable",
        },
    )
    _run(repository, "init", "-q", "-b", "main")
    _run(repository, "config", "user.email", "worker@example.invalid")
    _run(repository, "config", "user.name", "Worker")
    _commit(repository, "chore: seed the plan")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _store_review(path: Path, *, run_id: str) -> dict:
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
    from reckon import _plan_html

    sidecard["plan_version"] = int(
        _plan_html.read_state(path.read_text(encoding="utf-8"))["version"]
    )
    Path(sidecard["report_path"]).write_text("RUBRIC wiring: pass\n", encoding="utf-8")
    plan_review.store_plan_review(
        {**sidecard, "findings": [], "responses": {}, "review_run_id": run_id}
    )
    return sidecard


def _config_for():
    def resolve(pointer):
        return CONFIG

    return resolve


def _node(node_id: str, section: str, **extra) -> TaskNode:
    return TaskNode(
        id=node_id,
        goal=f"build {section}",
        plan=PLAN,
        section=section,
        done_when=f"pytest reports passed for {section}",
        write_paths=[f"{section}.txt"],
        time_budget="20m",
        spec_level="exact",
        **extra,
    )


def _launcher(recorded: list[str]):
    def launch(plan, *, log_path, stderr_path, prompt_path):
        recorded.append(Path(str(prompt_path)).name)
        return os.getpid()

    return launch


def _live() -> dict[str, dict]:
    return {
        str(pointer["run_id"]): pointer for pointer in runs.list_live(project=PROJECT)
    }


def _pointer(run_id: str) -> dict:
    return runs.read_pointer(run_id)


def _edit_done_when(path: Path, section: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    before = DONE_WHEN[section]
    assert before in text, "the fixture always writes the done-when it edits"
    path.write_text(text.replace(before, replacement, 1), encoding="utf-8")


def _queue(repository: Path, entries, *, after=None):
    """Queue one request per ``(run_id, section)`` pair, in the order given."""
    nodes = []
    ids: dict[str, str] = {}
    for run_id, section in entries:
        node_id = run_id.replace("r-", "n-")
        extra = {"after": list(after[run_id])} if after and run_id in after else {}
        nodes.append(_node(node_id, section, **extra))
        ids[node_id] = run_id
    return queue_admission.queue_requests(
        nodes,
        project=PROJECT,
        session=SESSION,
        repo_root=repository,
        config=CONFIG,
        run_ids=ids,
    )


def test_an_edit_elsewhere_lets_its_neighbour_launch(project: Path) -> None:
    """Editing one section's prose must not strand another section's request."""
    path = project / "docs" / "plans" / f"{PLAN}.html"
    _store_review(path, run_id="review-one")
    _queue(project, [("r-s2", "s2"), ("r-s3", "s3")])
    assert _pointer("r-s2")["phase"] == queue_admission.QUEUED_PHASE
    assert _pointer("r-s3")["phase"] == queue_admission.QUEUED_PHASE

    _edit_done_when(path, "s3", "charlie ships and nothing breaks")
    _commit(project, "chore: steer section three")

    launched: list[str] = []
    outcome = queue_admission.admit_once(
        config_for=_config_for(), launcher=_launcher(launched)
    )

    assert len(launched) == 1, (
        f"the section whose units the review covers launches: {outcome} {launched}"
    )
    held = _pointer("r-s3")
    assert held["phase"] == queue_admission.HELD_PHASE
    assert held["queue_hold"]["reason"] == queue_admission.PLAN_REVIEW_HELD_REASON
    assert held["queue_hold"]["automatic"] is True
    assert "uncovered units: s3" in held["queue_hold"]["detail"], held["queue_hold"]

    view = obligations_module.obligations(PROJECT, SESSION)
    rows = [row for row in view["obligations"] if row.get("kind") == "plan-review-owed"]
    assert len(rows) == 1, "one row for the plan, not one per request"
    assert rows[0]["plan"] == PLAN
    assert rows[0]["held_count"] == 1
    assert "reckon crew review" in rows[0]["next_command"]
    assert f"--plan {PLAN}" in rows[0]["next_command"]

    _store_review(path, run_id="review-two")
    launched.clear()
    result = queue_admission.admit_once(
        config_for=_config_for(), launcher=_launcher(launched)
    )

    assert result["released_plan_review"] == ["r-s3"]
    assert len(launched) == 1, "the stored review releases it with no coordinator call"


def test_an_edit_holding_two_sections_reads_as_one_plan_row(project: Path) -> None:
    path = project / "docs" / "plans" / f"{PLAN}.html"
    _store_review(path, run_id="review-one")
    _queue(project, [("r-s2", "s2"), ("r-s3", "s3"), ("r-s4", "s4")])

    _edit_done_when(path, "s3", "charlie ships and nothing breaks")
    _edit_done_when(path, "s4", "delta ships and nothing breaks")
    _commit(project, "chore: steer two sections")

    queue_admission.admit_once(config_for=_config_for(), launcher=_launcher([]))

    view = obligations_module.obligations(PROJECT, SESSION)
    rows = [row for row in view["obligations"] if row.get("kind") == "plan-review-owed"]
    assert len(rows) == 1
    assert rows[0]["held_count"] == 2


def test_a_hand_held_request_stays_held_through_the_releasing_pass(
    project: Path,
) -> None:
    from reckon.crew import queue_edit

    path = project / "docs" / "plans" / f"{PLAN}.html"
    _store_review(path, run_id="review-one")
    _queue(project, [("r-s4", "s4")])
    queue_edit.hold("r-s4", reason="hold this one while I think")
    assert _pointer("r-s4")["phase"] == queue_admission.HELD_PHASE

    _edit_done_when(path, "s4", "delta ships and nothing breaks")
    _commit(project, "chore: steer section four")
    queue_admission.admit_once(config_for=_config_for(), launcher=_launcher([]))

    _store_review(path, run_id="review-two")
    result = queue_admission.admit_once(
        config_for=_config_for(), launcher=_launcher([])
    )

    pointer = _pointer("r-s4")
    assert pointer["phase"] == queue_admission.HELD_PHASE
    assert not (pointer.get("queue_hold") or {}).get("automatic")
    assert "r-s4" not in result["released_plan_review"]


def test_a_narrowed_wait_releases_a_waiting_request(project: Path) -> None:
    path = project / "docs" / "plans" / f"{PLAN}.html"
    declarations = {
        "s2": "done",
        "s3": "implementable",
        "s4": "implementable",
        "s5": "implementable",
    }
    _write_plan(project, declarations, {"s3": [f"{PLAN}#s2", f"{PLAN}#s4"]})
    _commit(project, "chore: declare the waits")
    _store_review(path, run_id="review-one")

    stored = _queue(project, [("r-s3", "s3")])
    assert stored["r-s3"]["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE
    assert stored["r-s3"]["waits"] == [f"{PLAN}#s2", f"{PLAN}#s4"]

    _write_plan(project, declarations, {"s3": [f"{PLAN}#s2"]})
    _commit(project, "chore: narrow the waits")

    launched: list[str] = []
    queue_admission.admit_once(config_for=_config_for(), launcher=_launcher(launched))

    assert len(launched) == 1, "the plan's narrowed wait releases the request"
    assert _pointer("r-s3")["phase"] not in queue_admission.WAITING_UPSTREAM_PHASES


def test_a_narrowed_wait_rewrites_the_stored_refs_before_launch(
    project: Path,
) -> None:
    from reckon.crew.queue_admission import derive_wait_refs

    path = project / "docs" / "plans" / f"{PLAN}.html"
    declarations = {
        "s2": "done",
        "s3": "implementable",
        "s4": "implementable",
        "s5": "implementable",
    }
    _write_plan(project, declarations, {"s3": [f"{PLAN}#s2", f"{PLAN}#s4"]})
    _commit(project, "chore: declare the waits")
    _store_review(path, run_id="review-one")
    _queue(project, [("r-s3", "s3")])

    _write_plan(project, declarations, {"s3": [f"{PLAN}#s2"]})
    _commit(project, "chore: narrow the waits")

    queue_admission.advance_waiting_requests(runs.list_live())

    pointer = _pointer("r-s3")
    assert pointer["phase"] == queue_admission.QUEUED_PHASE
    assert pointer["waits"] == [f"{PLAN}#s2"]
    assert pointer["waits"] == derive_wait_refs(_node("n-s3", "s3"), repo_root=project)


def test_an_after_ref_survives_the_same_pass(project: Path) -> None:
    path = project / "docs" / "plans" / f"{PLAN}.html"
    _store_review(path, run_id="review-one")
    _queue(project, [("r-up", "s5")])
    queued = _queue(project, [("r-after", "s5")], after={"r-after": ["r-up"]})
    assert queued["r-after"]["waits"] == ["run:r-up"]

    queue_admission.advance_waiting_requests(runs.list_live())

    pointer = _pointer("r-after")
    assert pointer["waits"] == ["run:r-up"], "the request's own --after ref is kept"
    assert pointer["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE


def test_a_widened_wait_sends_a_queued_request_back(project: Path) -> None:
    path = project / "docs" / "plans" / f"{PLAN}.html"
    declarations = {
        "s2": "done",
        "s3": "implementable",
        "s4": "implementable",
        "s5": "implementable",
    }
    _write_plan(project, declarations)
    _commit(project, "chore: seed the declarations")
    _store_review(path, run_id="review-one")
    _queue(project, [("r-s4", "s4")])
    assert _pointer("r-s4")["phase"] == queue_admission.QUEUED_PHASE

    _write_plan(project, declarations, {"s4": [f"{PLAN}#s2", f"{PLAN}#s5"]})
    _commit(project, "chore: widen the waits")

    queue_admission.advance_waiting_requests(runs.list_live())

    pointer = _pointer("r-s4")
    assert pointer["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE
    assert pointer["waits"] == [f"{PLAN}#s2", f"{PLAN}#s5"]


def test_a_blocked_upstream_keeps_its_waiter_waiting(project: Path) -> None:
    """A worker that stopped for help has not abandoned its section.

    ``blocked`` is a manifest status the run can still be resumed or repaired
    from, so a request waiting on it keeps waiting; only ``failed`` (and a
    discard, below) closes the wait as upstream-abandoned.
    """
    _store_review(project / "docs" / "plans" / f"{PLAN}.html", run_id="review-one")
    _queue(project, [("r-up", "s5")])
    _queue(project, [("r-wait", "s5")], after={"r-wait": ["r-up"]})

    manifest = runs.run_dir("r-up") / runs.DEFAULT_MANIFEST_NAME
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "status: blocked\nblockers: the work did not finish\n", encoding="utf-8"
    )

    queue_admission.advance_waiting_requests(runs.list_live())
    assert _pointer("r-wait")["phase"] == (queue_admission.AWAITING_UPSTREAM_PHASE), (
        "a blocked manifest never satisfies a wait and never abandons one"
    )

    manifest.write_text("status: failed\n", encoding="utf-8")
    queue_admission.advance_waiting_requests(runs.list_live())
    assert _pointer("r-wait")["phase"] == (queue_admission.BLOCKED_UPSTREAM_PHASE), (
        "a failed manifest is a closure"
    )


def test_a_discarded_upstream_blocks_its_waiter(project: Path) -> None:
    from reckon.crew import promotion

    _store_review(project / "docs" / "plans" / f"{PLAN}.html", run_id="review-one")
    _queue(project, [("r-up", "s5")])
    _queue(project, [("r-wait", "s5")], after={"r-wait": ["r-up"]})

    marker = promotion.discard_record_path("r-up")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}\n", encoding="utf-8")

    queue_admission.advance_waiting_requests(runs.list_live())
    assert _pointer("r-wait")["phase"] == queue_admission.BLOCKED_UPSTREAM_PHASE
