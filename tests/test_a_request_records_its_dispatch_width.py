"""A stored request records the width it was dispatched at.

The lead asked whether sessions dispatch at full plan width, and no record could
answer: a request said how it was launched and admitted, never whether it came
from a call naming its whole plan, a batch file or one section. Three call paths
over a fixture project store a request each, and each is checked on the pointer
the queue writes, on the live view read back by that field, and on the ledger
row promotion commits.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import dispatch_batch, runs
from reckon.crew.dispatch_admission import LaneHeld
from reckon.crew.node import TaskNode
from reckon.crew.queue_view import queue_view
from reckon.crew.runs import pointer_path, run_dir

PROJECT = "proj"
SESSION = "coordinator-a"
PLAN = "plan-a"
OTHER_PLAN = "plan-b"

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

FIXTURE_WAIVER = "fixture: the width a request records is the subject"


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
        f"<meta name={quote}plan-section-declarations{quote} content={quote}"
        f"{declarations}{quote}>\n"
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
        _plan_html(PLAN, {"s1": "", "s2": "", "s3": ""}), encoding="utf-8"
    )
    (plans / f"{OTHER_PLAN}.html").write_text(
        _plan_html(OTHER_PLAN, {"s1": "", "s2": "", "s3": ""}), encoding="utf-8"
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


def _batch_file(repository: Path) -> dict:
    entries = [
        {
            "project": PROJECT,
            "plan": OTHER_PLAN,
            "section": f"s{index}",
            "node": f"{OTHER_PLAN}-s{index}",
            "goal": f"build {index}",
            "spec_level": "exact",
            "time_budget": "20m",
        }
        for index in (1, 2)
    ]
    return dispatch_batch.queue_batch(
        entries,
        session=SESSION,
        config_for=_config_for,
        repo_root=repository,
        dispatch_width="batch",
    )


def _single_section(repository: Path, upstream_run_id: str) -> dict:
    node = TaskNode(
        id="one-section",
        plan=OTHER_PLAN,
        section="s3",
        goal="build one section",
        done_when="the section's check passes",
        write_paths=[],
        after=[upstream_run_id],
        manifest_path=str(run_dir("r-one-section") / "manifest.md"),
        spec_level="exact",
        time_budget="20m",
    )
    try:
        return crew.dispatch(
            node=node,
            project=PROJECT,
            repo=repository,
            config=CONFIG,
            session=SESSION,
            check_budget=True,
            launcher=lambda *_args, **_kwargs: os.getpid(),
        )
    except LaneHeld as held:
        # A request waiting on another live request is stored rather than
        # dispatched, and the hold names the request it stored.
        return dict(held.queued or {})


def _pointer(run_id: str) -> dict:
    return runs.read_pointer(run_id)


def _gate_check(log: Path) -> dict:
    log.write_text("the check ran\nEXIT=0\n", encoding="utf-8")
    return {"command": "true", "exit_status": 0, "log_path": str(log)}


def _ledger_row(repository: Path, run_id: str) -> dict:
    data, _version = ledger.load(PROJECT, root=repository)
    row = next(
        (item for item in data["runs"] if str(item.get("run_id")) == run_id), None
    )
    assert row is not None, f"promotion committed no ledger row for {run_id}"
    return dict(row)


def test_each_call_path_stores_its_own_width(repository: Path) -> None:
    """A whole-plan call, a batch file and one section each record their width."""
    plan_result = _whole_plan(repository)
    print(json.dumps(plan_result) if False else plan_result)
    plan_requests = [row for row in plan_result["results"] if row["status"] == "queued"]
    assert len(plan_requests) == 3, plan_result
    for row in plan_requests:
        assert _pointer(row["run_id"]).get("dispatch_width") == "plan", row

    batch_result = _batch_file(repository)
    batch_requests = [
        row for row in batch_result["results"] if row["status"] == "queued"
    ]
    assert len(batch_requests) == 2, batch_result
    for row in batch_requests:
        assert _pointer(row["run_id"]).get("dispatch_width") == "batch", row

    single = _single_section(repository, plan_requests[0]["run_id"])
    single_pointer = _pointer(single["run_id"])
    assert single_pointer.get("phase") in {
        "queued",
        "awaiting-upstream",
        "blocked-upstream",
    }, single_pointer
    assert single_pointer.get("dispatch_width") == "section"

    # The live view read back by that field returns exactly the requests that
    # carries it, and the unfiltered view names each request's width.
    plan_ids = {row["run_id"] for row in plan_requests}
    batch_ids = {row["run_id"] for row in batch_requests}
    waiting = queue_view(PROJECT)
    assert {row["run_id"] for row in waiting["requests"]} == (
        plan_ids | batch_ids | {single["run_id"]}
    )
    assert {row["run_id"]: row["dispatch_width"] for row in waiting["requests"]} == {
        **dict.fromkeys(plan_ids, "plan"),
        **dict.fromkeys(batch_ids, "batch"),
        single["run_id"]: "section",
    }
    for width, expected in (
        ("plan", plan_ids),
        ("batch", batch_ids),
        ("section", {single["run_id"]}),
    ):
        filtered = queue_view(PROJECT, dispatch_width=width)["requests"]
        assert {row["run_id"] for row in filtered} == expected, width
        assert all(row["dispatch_width"] == width for row in filtered)


def _land_the_request(repository: Path, run_id: str, outcome: str) -> None:
    """Take a stored request through the landing it would reach after running.

    Promotion reads the pointer the dispatch wrote, so the pointer's own record
    is kept and only the fields a worker's completion supplies are added.
    """
    pointer = _pointer(run_id)
    # The launch a queued request would have had: promotion reads the pointer
    # the dispatch wrote, so its record is kept and only what a worker's run
    # supplies is added.
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


def test_promotion_carries_each_width_onto_the_ledger_row(
    repository: Path,
) -> None:
    """Each call path's request reaches the ledger row with its own width."""
    plan_result = _whole_plan(repository)
    batch_result = _batch_file(repository)
    single = _single_section(repository, plan_result["results"][0]["run_id"])

    targets = {
        "plan": plan_result["results"][0]["run_id"],
        "batch": batch_result["results"][0]["run_id"],
        "section": single["run_id"],
    }
    for width, run_id in targets.items():
        assert _pointer(run_id).get("dispatch_width") == width
        _land_the_request(repository, run_id, f"the {width} request landed")
        assert _ledger_row(repository, run_id)["dispatch_width"] == width
