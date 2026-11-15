"""Each dispatch-family verb writes one JSON document to stdout.

A caller pipes these verbs and decodes the whole of stdout, so an exit path
that writes nothing — a parsing refusal, a raised refusal, a helper's notice —
leaves the caller with nothing to read. Every outcome therefore answers with
exactly one document, and a request the call itself stores reports itself as
created rather than naming the run back as one that was already there.

The verbs run as real subprocesses with stdout piped, because a stream that is
a terminal while the command runs is not the stream a caller decodes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = "proj"
PLAN = "plan-a"
SESSION = "coordinator-one"

FLIGHT = """
default_backend: worker
backends:
  worker:
    launch: cli
    command: codex
    model: some-model
    effort: high
    sandbox: worktree-full
    session_reuse: true
    time_budget: 2h
roles:
  implement: {}
fences:
  time_budget: 2h
  needs_help_after_failures: 2
"""

PLAN_HTML = """<!doctype html>
<meta name="docs-project" content="proj">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="plan-a">
<meta name="plan-status" content="active">
<meta name="plan-section-declarations" content="{declarations}">
<meta name="plan-section-depends-on" content="{waits}">
<h2 id="s1">§1</h2>
<section data-reckon="section" data-id="s1" data-effort-hours="1.0" data-capability-class="general" data-capability-reasoning="standard" data-capability-verification="standard" data-capability-risk="low" data-status="implementable"></section>
<h2 id="s2">§2</h2>
<section data-reckon="section" data-id="s2" data-effort-hours="1.0" data-capability-class="general" data-capability-reasoning="standard" data-capability-verification="standard" data-capability-risk="low" data-status="implementable"></section>
"""


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


@pytest.fixture()
def crew_repo(tmp_path: Path) -> dict:
    """A throwaway crew home and checkout with one plan and one dependency."""
    home = tmp_path / "config"
    home.mkdir()
    root = tmp_path / "checkout"
    scripts = root / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills" / "reckon-build" / "scripts"
    (scripts / "worktree_fleet.py").write_text(
        (source / "worktree_fleet.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    state = root / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    (state / "flight.yaml").write_text(FLIGHT, encoding="utf-8")
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    declarations = json.dumps({"s1": "implementable", "s2": "implementable"})
    waits = json.dumps({"s2": [f"{PLAN}#s1"]})
    (plans / f"{PLAN}.html").write_text(
        PLAN_HTML.replace(
            "{declarations}", declarations.replace('"', "&quot;")
        ).replace("{waits}", waits.replace('"', "&quot;")),
        encoding="utf-8",
    )
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    _git(root, "add", "seed.txt", "docs", "skills")
    _git(root, "commit", "-q", "-m", "chore: seed the fixture")
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return {"home": home, "root": root}


def _run(fixture: dict, *arguments: str) -> subprocess.CompletedProcess:
    """Run the console script with stdout piped, as a caller does."""
    interpreter = Path(sys.executable)
    console = interpreter.with_name("reckon")
    environment = dict(os.environ)
    environment["RECKON_HOME"] = str(fixture["home"])
    environment["PYTHONPATH"] = str(Path(__file__).parents[1])
    for name in ("RECKON_FLIGHT_CONFIG", "RECKON_WORKTREES"):
        environment.pop(name, None)
    return subprocess.run(
        [str(console), *arguments],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(fixture["root"]),
        env=environment,
    )


def _one_document(result: subprocess.CompletedProcess) -> dict:
    """Parse the whole of stdout as the single document the caller reads."""
    return json.loads(result.stdout)


def _dispatch(fixture: dict, *extra: str, node: str, section: str) -> list[str]:
    return [
        "crew",
        "dispatch",
        "--project",
        PROJECT,
        "--plan",
        PLAN,
        "--section",
        section,
        "--node",
        node,
        "--goal",
        "take the next step",
        "--repo",
        str(fixture["root"]),
        "--session",
        SESSION,
        "--done-when",
        "the gate command exits 0",
        "--spec-level",
        "guided",
        "--time-budget",
        "2h",
        "--backend",
        "worker",
        *extra,
    ]


def test_a_successful_dispatch_answers_with_one_document(crew_repo: dict) -> None:
    result = _run(
        crew_repo, *_dispatch(crew_repo, node="head", section="s1"), "--dry-run"
    )

    document = _one_document(result)
    assert result.returncode == 0, result.stderr
    assert document["ok"] is True
    assert document["dry_run"] is True


def test_a_refused_dispatch_answers_with_one_document(crew_repo: dict) -> None:
    result = _run(
        crew_repo,
        *_dispatch(crew_repo, node="refused", section="s1"),
        "--dry-run",
        "--backend",
        "no-such-lane",
    )

    document = _one_document(result)
    assert result.returncode != 0
    assert document["ok"] is False
    assert document["error"]


def test_a_raised_refusal_answers_with_one_document(crew_repo: dict) -> None:
    """A verb that raises still writes its caller the document they decode."""
    result = _run(
        crew_repo, "crew", "complete", "--run", "r-not-a-run", "--gate", "passed"
    )

    document = _one_document(result)
    assert result.returncode != 0
    assert document["ok"] is False
    assert "r-not-a-run" in document["detail"]


def test_a_parsing_refusal_answers_with_one_document(crew_repo: dict) -> None:
    """A refusal raised while the arguments are parsed is answered too."""
    result = _run(crew_repo, "crew", "complete", "--run", "r-not-a-run")

    document = _one_document(result)
    assert result.returncode != 0
    assert document["ok"] is False


def test_a_batch_dispatch_answers_with_one_document(
    crew_repo: dict, tmp_path: Path
) -> None:
    batch = tmp_path / "batch.json"
    batch.write_text(
        json.dumps(
            [
                {
                    "project": PROJECT,
                    "plan": PLAN,
                    "section": PLAN + "#s1",
                    "node": "batched",
                    "goal": "take the next step",
                    "done_when": "the gate command exits 0",
                    "spec_level": "guided",
                    "time_budget": "2h",
                }
            ]
        ),
        encoding="utf-8",
    )

    result = _run(
        crew_repo,
        "crew",
        "dispatch",
        "--batch",
        str(batch),
        "--project",
        PROJECT,
        "--repo",
        str(crew_repo["root"]),
        "--session",
        SESSION,
        "--backend",
        "worker",
    )

    # The batch answers one document per call on either outcome; whether an entry
    # is accepted is the entry's own contract, not this verb's channel.
    document = _one_document(result)
    assert len(document["results"]) == 1


def test_a_review_dispatch_answers_with_one_document(crew_repo: dict) -> None:
    result = _run(
        crew_repo,
        "crew",
        "review",
        "--project",
        PROJECT,
        "--plan",
        PLAN,
        "--session",
        SESSION,
        "--dry-run",
    )

    document = _one_document(result)
    assert document, result.stderr


def test_a_fresh_request_behind_an_upstream_is_created_not_already(
    crew_repo: dict,
) -> None:
    """The first dispatch of a waiting node reports the request it created."""
    arguments = [
        *_dispatch(crew_repo, node="waits", section="s2"),
        "--no-watch",
    ]
    result = _run(crew_repo, *arguments)

    document = _one_document(result)
    assert result.returncode == 0, result.stderr
    assert document["queued"] is True
    assert document["phase"] == "awaiting-upstream"
    assert "already" not in str(document.get("detail") or "").lower()

    # The run it names is the pointer this call wrote, so the answer is about
    # a request that did not exist before the call.
    pointer_path = crew_repo["home"] / "crew" / "live" / f"{document['run_id']}.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    assert pointer["phase"] == "awaiting-upstream"
    assert str((pointer.get("node") or {}).get("id")) == "waits"
