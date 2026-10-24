"""A queued request's brief binds at launch, from its own steering file.

A request's authority text lives in ``steering.md`` in its own run directory.
The queue writes it once, from the dispatch's brief, and never overwrites it:
a coordinator replaces that file while the request waits, and the launch reads
whatever it holds at that moment rather than the brief text the queued pointer
carries. A steering file that no longer reads as authority text is refused
inside the admission path, and the request stays queued with the failure named
and a retry time.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import queue_admission, runs
from reckon.crew.node import TaskNode

PROJECT = "proj"
SESSION = "coordinator-a"
SECTION = "dispatch"

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


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


@pytest.fixture()
def queue_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repository = tmp_path / "checkout"
    scripts = repository / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills" / "reckon-build" / "scripts"
    (scripts / "worktree_fleet.py").write_text(
        (source / "worktree_fleet.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    state = repository / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    quote = '"'
    plan = repository / "docs" / "plans" / "plan-a.html"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        "\n".join(
            [
                "<!doctype html>",
                f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>",
                f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>",
                f"<meta name={quote}plan-slug{quote} content={quote}plan-a{quote}>",
                f"<h2 id={quote}{SECTION}{quote}>Dispatch</h2>",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    _git(repository, "add", "--", "docs")
    _git(repository, "add", "--", "skills")
    _git(repository, "commit", "-q", "-m", "chore: seed checkout")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _config_for():
    config = json.loads(json.dumps(CONFIG))

    def resolve(_pointer):
        return config

    return config, resolve


def _request(
    repository: Path,
    *,
    node_id: str,
    run_id: str,
    brief: str = "",
    brief_path: str = "",
) -> dict:
    node = TaskNode(
        id=node_id,
        goal="launch this queued node",
        plan="plan-a",
        section=SECTION,
        brief=brief,
        brief_path=brief_path,
        done_when="pytest tests/test_queue_steering_binding.py reports passed",
        write_paths=[f"{node_id}.txt"],
        time_budget="20m",
        spec_level="exact",
    )
    joined = datetime.now(UTC).isoformat()
    return {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repository),
        "node": node.as_dict(),
        "backend": "clive",
        "launch": "cli",
        "local": True,
        "phase": "queued",
        "queued_at": joined,
        "created_at": joined,
        "hold": {"held": True},
        "dispatch_options": {
            "project": PROJECT,
            "plan_slug": "plan-a",
            "section": SECTION,
            "session": SESSION,
            "local": True,
            "no_watch": True,
            "overrides": [],
        },
    }


def _launcher(recorded: list):
    def launch(plan, *, log_path, stderr_path, prompt_path):
        recorded.append(plan)
        return os.getpid()

    return launch


def _source(tmp_path: Path, text: str, name: str = "brief-source.md") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _steering(run_id: str) -> Path:
    return runs.run_dir(run_id) / runs.STEERING_FILE


def test_queue_dispatch_stores_its_brief_as_steering(
    queue_repo: Path, tmp_path: Path
) -> None:
    source = _source(tmp_path, "steer this run\n")
    runs.queue_dispatch(
        _request(
            queue_repo,
            node_id="steered",
            run_id="r-steered",
            brief=str(source),
            brief_path=str(source),
        )
    )

    stored = _steering("r-steered")
    assert stored.read_text(encoding="utf-8") == "steer this run\n"


def test_a_requeue_in_place_keeps_the_edited_steering(
    queue_repo: Path, tmp_path: Path
) -> None:
    source = _source(tmp_path, "queue-time text\n")
    request = _request(
        queue_repo,
        node_id="edited",
        run_id="r-edited",
        brief=str(source),
        brief_path=str(source),
    )
    runs.queue_dispatch(request)
    _steering("r-edited").write_text("edited while waiting\n", encoding="utf-8")

    replacement = _source(tmp_path, "a newer brief\n", name="newer.md")
    again = _request(
        queue_repo,
        node_id="edited",
        run_id="r-edited-fresh",
        brief=str(replacement),
        brief_path=str(replacement),
    )
    runs.queue_dispatch(again)

    assert _steering("r-edited").read_text(encoding="utf-8") == "edited while waiting\n"
    # The in-place re-queue keeps the existing request rather than minting a
    # second one, so the edited file remains the one its launch reads.
    assert not _steering("r-edited-fresh").exists()


def test_a_launch_reads_the_replaced_steering_file(
    queue_repo: Path, tmp_path: Path
) -> None:
    source = _source(tmp_path, "queue-time text\n")
    runs.queue_dispatch(
        _request(
            queue_repo,
            node_id="replaced",
            run_id="r-replaced",
            brief=str(source),
            brief_path=str(source),
        )
    )
    replaced = "replaced before launch\n"
    _steering("r-replaced").write_text(replaced, encoding="utf-8")
    recorded: list = []
    _, resolve = _config_for()

    result = queue_admission.admit_once(
        config_for=resolve, launcher=_launcher(recorded)
    )

    launched = runs.read_pointer(result["launched"][0])
    assert (
        launched["node"]["brief_sha256"]
        == hashlib.sha256(replaced.encode("utf-8")).hexdigest()
    )
    assert launched["brief"]["sha256"] == launched["node"]["brief_sha256"]


def test_a_launch_ignores_the_pointer_brief_when_steering_exists(
    queue_repo: Path, tmp_path: Path
) -> None:
    source = _source(tmp_path, "steering text\n")
    runs.queue_dispatch(
        _request(
            queue_repo,
            node_id="discriminating",
            run_id="r-discriminating",
            brief=str(source),
            brief_path=str(source),
        )
    )
    # The file the queued pointer's node.brief names changes; steering.md is
    # left exactly as the queue wrote it. The launch must read the steering
    # file, so its digest matches that text and not the pointer's.
    source.write_text("pointer text after the queue\n", encoding="utf-8")
    _, resolve = _config_for()

    result = queue_admission.admit_once(config_for=resolve, launcher=_launcher([]))

    launched = runs.read_pointer(result["launched"][0])
    steering_digest = hashlib.sha256(b"steering text\n").hexdigest()
    assert launched["node"]["brief_sha256"] == steering_digest
    assert (
        launched["node"]["brief_sha256"]
        != hashlib.sha256(b"pointer text after the queue\n").hexdigest()
    )


def test_an_invalid_steering_file_keeps_the_request_queued(
    queue_repo: Path, tmp_path: Path
) -> None:
    source = _source(tmp_path, "queue-time text\n")
    runs.queue_dispatch(
        _request(
            queue_repo,
            node_id="invalid",
            run_id="r-invalid",
            brief=str(source),
            brief_path=str(source),
        )
    )
    _steering("r-invalid").write_text("   \n", encoding="utf-8")
    recorded: list = []
    _, resolve = _config_for()

    result = queue_admission.admit_once(
        config_for=resolve, launcher=_launcher(recorded)
    )

    assert result["failed"] == ["r-invalid"]
    assert recorded == []
    pointer = runs.read_pointer("r-invalid")
    assert pointer["phase"] == "queued"
    assert "carries no authority text" in pointer["failure"]
    assert datetime.fromisoformat(pointer["retry_at"]) > datetime.now(UTC)


def test_a_plan_section_alone_has_no_steering_and_launches(
    queue_repo: Path,
) -> None:
    runs.queue_dispatch(
        _request(queue_repo, node_id="section-only", run_id="r-section")
    )
    recorded: list = []
    _, resolve = _config_for()

    result = queue_admission.admit_once(
        config_for=resolve, launcher=_launcher(recorded)
    )

    assert not _steering("r-section").exists()
    launched = runs.read_pointer(result["launched"][0])
    assert launched["phase"] != "queued"
    assert launched["node"]["brief"] == ""
    assert len(recorded) == 1
