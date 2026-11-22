"""A plan-level dispatch does not queue a section whose run has landed.

A section is declared implementable for the minute between its landing and the
landing's closing write, and by then the run is settled: its live pointer is
retired and its ledger row says it landed. A plan-level dispatch reads the
declaration alone in that minute and queues the section again, so the second
request duplicates work already on the primary branch. The pairs below hold
each side: a landed section is skipped naming its run, and a section whose only
run was discarded is queued, because a discarded run delivered nothing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import dispatch_batch, runs
from reckon.crew.runs import pointer_path

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
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}

FIXTURE_WAIVER = "fixture: the queue decision is the subject"


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "--no-pager", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
    )


def _plan_html(slug: str, sections: dict[str, str]) -> str:
    quote = '"'
    declarations = json.dumps(dict.fromkeys(sections, "implementable"))
    declarations = declarations.replace(quote, "&quot;")
    body = "".join(
        f"<h2 id={quote}{name}{quote}>§{name[1:]}</h2>"
        f"<p>Build {name}.</p>"
        f"<p><strong>Done when</strong> the {name} check passes.</p>"
        for name in sections
    )
    return (
        "<!doctype html>\n"
        f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>\n"
        f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>\n"
        f"<meta name={quote}plan-slug{quote} content={quote}{slug}{quote}>\n"
        f"<meta name={quote}plan-status{quote} content={quote}active{quote}>\n"
        f"<meta name={quote}plan-section-declarations"
        f"{quote} content={quote}{declarations}{quote}>\n"
        f"{body}\n"
    )


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    root = tmp_path / "checkout"
    scripts = root / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills" / "reckon-build" / "scripts"
    (scripts / "worktree_fleet.py").write_text(
        (source / "worktree_fleet.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "state" / PROJECT / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{PLAN}.html").write_text(
        _plan_html(PLAN, {"s1": "", "s2": ""}), encoding="utf-8"
    )
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    _git(root, "add", "--", "docs", "skills")
    _git(root, "commit", "-q", "-m", "chore: seed checkout")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _config_for(_project: str) -> dict:
    return json.loads(json.dumps(CONFIG))


def _whole_plan(repository: Path) -> dict:
    return dispatch_batch.queue_plan(
        PROJECT,
        PLAN,
        session=SESSION,
        config_for=_config_for,
        repo_root=repository,
    )


def _queue_section(repository: Path, section: str) -> str:
    """Queue one section under the node id its own dispatch names.

    A section dispatched on its own carries the node id that dispatch names,
    which is not the plan-level node id a whole-plan call derives from the
    section. Nothing in the plan ties the two together, so a reader that keys
    whose work a section is on the plan-level id alone cannot see it.
    """
    result = dispatch_batch.queue_batch(
        [
            {
                "project": PROJECT,
                "plan": PLAN,
                "section": section,
                "node": f"a-queued-request-named-for-{section}",
                "goal": f"build {section}",
                "spec_level": "exact",
                "time_budget": "20m",
            }
        ],
        session=SESSION,
        config_for=_config_for,
        repo_root=repository,
    )
    assert result["results"][0]["status"] == "queued", result
    return str(result["results"][0]["run_id"])


def _gate_check(log: Path) -> dict:
    log.write_text("the check ran\nEXIT=0\n", encoding="utf-8")
    return {"command": "true", "exit_status": 0, "log_path": str(log)}


def _complete(repository: Path, run_id: str, outcome: str) -> None:
    """Take a stored request through the completion its worker reports."""
    pointer = runs.read_pointer(run_id)
    pointer.update({"phase": "complete", "argv": ["codex", "exec"]})
    runs._write_json(pointer_path(run_id), pointer)
    manifest = Path(
        str(
            pointer.get("manifest_path")
            or (pointer.get("node") or {}).get("manifest_path")
        )
    )
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: none\n", encoding="utf-8"
    )
    crew.complete(
        run_id,
        gate="passed",
        outcome=outcome,
        root=repository,
        gate_check=_gate_check(Path(manifest.parent) / "gate.log"),
        no_impl_change=FIXTURE_WAIVER,
    )


def _retire_documents(repository: Path) -> None:
    """Leave a landed run's record where production keeps it: in the store only.

    Run history is written to the durable run store, and the ledger aggregate
    and per-run documents under a checkout's state directory are no longer
    written there. Removing them is the shape of a checkout whose documents
    have been retired, and it is the shape a reader of documents alone cannot
    see a landed run in.
    """
    state = ledger.ledger_path(PROJECT, repository).parent
    documents = state / "runs"
    if documents.exists():
        shutil.rmtree(documents)
    aggregate = ledger.ledger_path(PROJECT, repository)
    if aggregate.exists():
        aggregate.unlink()


def test_a_landed_section_is_not_queued_again(repository: Path) -> None:
    """A landed, retired section is skipped by a plan-level dispatch, by name."""
    landed_run = _queue_section(repository, "s1")
    _complete(repository, landed_run, "the section landed")
    _retire_documents(repository)

    # The pointer is retired by the landing, so the live set cannot answer.
    assert landed_run not in {row.get("run_id") for row in runs.list_live()}
    # The run is recorded as landed and the store is what answers for it, so
    # the section is a delivered one and not work to queue again.
    stored = {str(row.get("run_id")) for row in ledger.picker_runs(PROJECT)}
    assert landed_run in stored, "the landed run is not in the run store"
    # The section is still declared implementable, which is the window.
    assert _declaration(repository, "s1") == "implementable"

    second = _whole_plan(repository)
    rows = {row["section"]: row for row in second["results"]}
    assert rows["s1"]["status"] == "skipped", second
    assert landed_run in str(rows["s1"].get("reason") or ""), second
    # The section beside it is still work, so the skip is the landed section's
    # and not the whole plan's.
    assert rows["s2"]["status"] == "queued", second


def test_a_section_whose_run_was_discarded_is_queued(repository: Path) -> None:
    """A discarded run delivered nothing, so its section is still work."""
    dropped = _queue_section(repository, "s2")
    crew.discard(dropped)
    _retire_documents(repository)

    second = _whole_plan(repository)
    rows = {row["section"]: row for row in second["results"]}
    assert rows["s2"]["status"] == "queued", second


def _declaration(repository: Path, section: str) -> str:
    from reckon import _plan_html
    from reckon.crew.dispatch_batch import _Plans

    read = _Plans(repository).read(PROJECT, PLAN)
    for item in _plan_html.derive_section_todos(read.document, read.state):
        if item["id"] == section:
            return str(item.get("declaration") or "")
    raise AssertionError(f"no section {section}")
