"""A request a batch queued can be edited while it waits.

A batch entry is stored with the manifest path its worker will deliver to, the
same path a single queued dispatch stores, so the contract check ``crew queue
edit`` applies judges a batch-made request on the same terms as any other. A
request stored without one can never be edited: the contract refuses it naming
``independently-verifiable``, and the only remaining remedy is to drop it and
lose its queue position.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.crew import dispatch_batch, runs

crew_dispatch_commands = pytest.importorskip("reckon.crew_dispatch_commands")
CREW = crew_dispatch_commands.crew

PROJECT = "proj"
SESSION = "coordinator-a"
PLAN = "plan-a"
DONE_WHEN = "pytest -q tests/test_a_batch_request_can_be_edited.py reports passed."

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
    "fences": {"time_budget": "2h", "needs_help_after_failures": 2},
}


def _plan_document() -> str:
    quote = '"'
    prose = (
        "<p>Store the path a batch entry's worker delivers to.</p>"
        "<p>Write paths: <code>src/one.py</code>.</p>"
        "<p><strong>Done when</strong> pytest reports the batch edit check "
        "passing.</p>"
    )
    return (
        "<!doctype html>\n"
        f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>\n"
        f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>\n"
        f"<meta name={quote}plan-slug{quote} content={quote}{PLAN}{quote}>\n"
        f"<meta name={quote}plan-status{quote} content={quote}active{quote}>\n"
        f"<h2 id={quote}s1{quote}>§1</h2>{prose}\n"
    )


def _batch() -> list[dict]:
    return [
        {
            "project": PROJECT,
            "plan": PLAN,
            "section": "s1",
            "node": "batch-editable",
            "goal": "Store the manifest path a batch entry delivers to.",
            "time_budget": "20m",
            "spec_level": "exact",
            "done_when": DONE_WHEN,
            "negative_control": "none: this request writes no check",
        }
    ]


def _run(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


@pytest.fixture()
def batch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: CONFIG
    )

    repository = tmp_path / "checkout"
    # The section names src/one.py, so a derived path there must resolve.
    (repository / "src").mkdir(parents=True)
    state = repository / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    plans = repository / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{PLAN}.html").write_text(_plan_document(), encoding="utf-8")
    _run(repository, "init", "-q", "-b", "main")
    _run(repository, "config", "user.email", "worker@example.invalid")
    _run(repository, "config", "user.name", "Worker")
    _run(repository, "add", "--", "docs")
    _run(repository, "commit", "-q", "-m", "chore: seed checkout")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _queue(repository: Path) -> dict:
    return dispatch_batch.queue_batch(
        _batch(),
        session=SESSION,
        config_for=lambda _project: json.loads(json.dumps(CONFIG)),
        repo_root=repository,
    )


def _queued(batch_repo: Path) -> str:
    result = _queue(batch_repo)
    assert result["refused"] == 0, result
    row = result["results"][0]
    assert row["status"] in {"queued", "awaiting-upstream"}, row
    return str(row["run_id"])


def _pointer(run_id: str) -> dict:
    return json.loads(runs.pointer_path(run_id).read_text(encoding="utf-8"))


def test_a_batch_entry_names_the_manifest_path_a_single_dispatch_would(
    batch_repo: Path,
) -> None:
    run_id = _queued(batch_repo)

    stored = _pointer(run_id)["node"]
    # Spelled from the run directory itself rather than through the ``run_dir``
    # helper the production code uses, so a redefinition of that helper cannot
    # move the stored path while this check still agrees with it.
    expected = Path(runs.runs_dir()) / run_id / "manifest.md"

    assert Path(stored["manifest_path"]) == expected, (
        "a queued batch entry names the same delivery path a single dispatch "
        f"stores, under its own run directory: {stored['manifest_path']}"
    )
    assert Path(stored["manifest_path"]).is_absolute()


def test_a_batch_made_request_is_edited_in_the_queue(batch_repo: Path) -> None:
    run_id = _queued(batch_repo)
    before = _pointer(run_id)
    manifest = before["node"]["manifest_path"]

    outcome = CliRunner().invoke(
        CREW, ["queue", "edit", "--run", run_id, "--time-budget", "90m"]
    )

    assert outcome.exit_code == 0, outcome.output
    stored = _pointer(run_id)
    assert stored["node"]["time_budget"] == "90m", "the edit was stored"
    assert stored["node"]["manifest_path"] == manifest, (
        "the edit keeps the delivery path the batch stored"
    )


def test_a_request_stored_without_a_manifest_path_cannot_be_edited(
    batch_repo: Path,
) -> None:
    run_id = _queued(batch_repo)
    pointer_path = runs.pointer_path(run_id)
    stored = json.loads(pointer_path.read_text(encoding="utf-8"))
    # The shape a batch entry had before this path was stored for it: the
    # request itself is complete, and only its delivery path is missing.
    stored["node"]["manifest_path"] = ""
    pointer_path.write_text(json.dumps(stored), encoding="utf-8")

    outcome = CliRunner().invoke(
        CREW, ["queue", "edit", "--run", run_id, "--time-budget", "90m"]
    )

    assert outcome.exit_code != 0, outcome.output
    assert "independently-verifiable" in outcome.output
    assert (
        json.loads(pointer_path.read_text(encoding="utf-8"))["node"]["time_budget"]
        == "20m"
    ), "a refused edit stores nothing"
