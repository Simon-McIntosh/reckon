"""A batch derives an absolute write path absolutely.

A section's write-paths sentence is the authority for a batch entry that gives
none. A path such as ``/abs/data/record.json`` must come back whole: without
its leading slash it becomes a repository-relative path that resolves nowhere,
the fence leaves the real directory read-only and the worker's write fails. A
relative path is derived as written, and a derived relative path that names
nothing under the dispatched repository is refused at queue time, naming the
path, rather than being queued to fail at the fence.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon.crew import dispatch_batch, runs

PROJECT = "proj"
SESSION = "coordinator-a"
PLAN = "plan-a"
ABSOLUTE_RECORD = "/abs/data/record.json"
RELATIVE_MODULE = "reckon/crew/x.py"
MISSING_RECORD = "docs/missing/record.json"
DONE_WHEN = (
    "pytest -q tests/test_a_batch_derives_an_absolute_write_path.py reports passed."
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
    "fences": {"time_budget": "2h", "needs_help_after_failures": 2},
}

SECTIONS = {
    "s1": ABSOLUTE_RECORD,
    "s2": RELATIVE_MODULE,
    "s3": MISSING_RECORD,
}


def _section(path: str) -> str:
    return (
        f"<p>The section writes one record.</p>"
        f"<p>Write paths: <code>{path}</code>.</p>"
        f"<p><strong>Done when</strong> pytest -q "
        f"tests/test_a_batch_derives_an_absolute_write_path.py reports 3 "
        f"passed.</p>"
    )


def _plan_document() -> str:
    quote = '"'
    body = "".join(
        f"<h2 id={quote}{name}{quote}>§{name[1:]}</h2>{_section(path)}"
        for name, path in SECTIONS.items()
    )
    return (
        "<!doctype html>\n"
        f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>\n"
        f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>\n"
        f"<meta name={quote}plan-slug{quote} content={quote}{PLAN}{quote}>\n"
        f"<meta name={quote}plan-status{quote} content={quote}active{quote}>\n"
        f"{body}\n"
    )


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
    (plans / f"{PLAN}.html").write_text(_plan_document(), encoding="utf-8")
    # The relative path's own directory exists; the missing one does not.
    (repository / "reckon" / "crew").mkdir(parents=True)
    (repository / RELATIVE_MODULE).write_text("", encoding="utf-8")
    _run(repository, "init", "-q", "-b", "main")
    _run(repository, "config", "user.email", "worker@example.invalid")
    _run(repository, "config", "user.name", "Worker")
    _run(repository, "add", "--", "docs", "reckon")
    _run(repository, "commit", "-q", "-m", "chore: seed checkout")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _derive(repository: Path, section: str) -> list[str]:
    plan = dispatch_batch._PlanRead(PROJECT, PLAN, repository)
    return plan.write_paths(section)


def _entry(section: str) -> dict:
    return {
        "project": PROJECT,
        "plan": PLAN,
        "section": section,
        "node": f"node-{section}",
        "goal": "Write the record the section names.",
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


def test_a_section_naming_an_absolute_path_derives_it_absolutely(
    batch_repo: Path,
) -> None:
    assert _derive(batch_repo, "s1") == [ABSOLUTE_RECORD]
    assert ABSOLUTE_RECORD.startswith("/")


def test_a_section_naming_a_relative_path_derives_it_unchanged(
    batch_repo: Path,
) -> None:
    assert _derive(batch_repo, "s2") == [RELATIVE_MODULE]


def test_a_derived_relative_path_that_resolves_nowhere_is_refused_at_queue_time(
    batch_repo: Path,
) -> None:
    result = _queue(batch_repo, "s3")

    assert result["refused"] == 1, result
    row = result["results"][0]
    assert row["status"] == "refused", row
    assert MISSING_RECORD in str(row["reason"]), row["reason"]
    assert runs.list_live(project=PROJECT) == [], "a refusal stores no pointer"
