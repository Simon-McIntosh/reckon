"""Static build output does not establish crew ownership; review history does."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

from reckon import cli_entry
from reckon.crew import routing
from reckon.crew.dispatching import comparisons
from reckon.hooks import native_agent_guard, worker_message_guard


@pytest.fixture(params=[False, True], ids=["static-site", "review-history"])
def project_repo(tmp_path: Path, request):
    repo = tmp_path / "repo"
    state = repo / "docs" / "state" / "sample"
    state.mkdir(parents=True)
    (repo / ".git").mkdir()
    (state / "index.json").write_text('{"project": "sample", "data": {}}\n')
    (state / "project.json").write_text('{"project": "sample"}\n')
    (state / "timeline.html").write_text("<main>Project timeline</main>\n")
    if request.param:
        reviews = state / "reviews" / "plan" / "example"
        reviews.mkdir(parents=True)
        (reviews / "review.json").write_text('{"status": "complete"}\n')
    assert not any(
        (state / name).exists() for name in ("crew.json", "flight.yaml", "runs")
    )
    return repo, request.param


@pytest.mark.parametrize("guard", [native_agent_guard, worker_message_guard])
def test_only_review_history_establishes_crew_ownership(project_repo, guard):
    repo, has_reviews = project_repo

    assert guard.crew_managed_projects(repo) == (["sample"] if has_reviews else [])


def test_native_agent_is_refused_only_with_crew_history(project_repo, monkeypatch):
    repo, has_reviews = project_repo
    monkeypatch.delenv(native_agent_guard.OVERRIDE_ENV, raising=False)
    monkeypatch.delenv(native_agent_guard.RUN_ID_ENV, raising=False)

    allowed, message = native_agent_guard.decide(
        {"tool_name": "Agent", "cwd": str(repo)}
    )

    assert allowed is not has_reviews
    if has_reviews:
        assert "refused" in message
        assert "--project sample" in message
    else:
        assert message is None


def test_peer_send_is_refused_only_with_crew_history(
    project_repo, tmp_path, monkeypatch
):
    repo, has_reviews = project_repo
    home = tmp_path / "config"
    live = home / "crew" / "live"
    live.mkdir(parents=True)
    (live / "worker.json").write_text(
        json.dumps(
            {
                "run_id": "r-worker",
                "launcher_host": socket.gethostname(),
                "pid": os.getpid(),
            }
        )
    )
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.delenv(worker_message_guard.OVERRIDE_ENV, raising=False)

    allowed, message = worker_message_guard.decide(
        {"tool_name": "SendMessage", "cwd": str(repo), "tool_input": {"to": "r-worker"}}
    )

    assert allowed is not has_reviews
    if has_reviews:
        assert "peer send refused" in message
        assert "r-worker" in message
    else:
        assert message is None


def test_cli_recognises_only_crew_history(project_repo):
    repo, has_reviews = project_repo

    assert cli_entry._crew_state_exists(repo / "docs", "sample") is has_reviews


def test_shadow_fallback_reads_only_a_crew_projects_ledger(project_repo, monkeypatch):
    repo, has_reviews = project_repo
    record = {
        "run_id": "r-primary",
        "base_sha": "0" * 40,
        "agent": {"backend": "local"},
        "node_definition": {
            "id": "example",
            "goal": "find the crew project",
            "plan": "example",
            "done_when": "the project resolves",
            "write_paths": ["example.py"],
        },
    }
    reads = []

    def runs(project, root=None):
        reads.append((project, root))
        return [record]

    monkeypatch.setattr(routing, "repository_projects", lambda path: ())
    monkeypatch.setattr(comparisons.ledger, "runs", runs)

    if has_reviews:
        assert comparisons.shadow_source("r-primary", repo=repo)["project"] == "sample"
    else:
        with pytest.raises(
            comparisons.CrewError, match="not a committed ledger record"
        ):
            comparisons.shadow_source("r-primary", repo=repo)
    assert reads == ([("sample", repo)] if has_reviews else [])
