"""A queued request waits on its dependencies and launches when they land.

A coordinator queues a plan's whole width in one call. Each request whose
section declares upstream refs is stored ``awaiting-upstream``; every admission
pass re-reads readiness through the roadmap's own derivation and moves the
request to ``queued`` as its last ref lands, with no coordinator turn between a
promotion and the downstream launch. A ref closed as abandoned or failed moves
the request to ``blocked-upstream``, which no pass launches, and blocking is
transitive so the tail of a chain is blocked with the middle.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from reckon.crew import queue_admission, runs
from reckon.crew.node import CrewError, TaskNode

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

# The chain every test queues: three sections, each waiting on the one before.
CHAIN = {"s2": [f"{PLAN}#s1"], "s3": [f"{PLAN}#s2"]}


def _run(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


def _plan_html(declarations: dict[str, str]) -> str:
    quote = '"'
    return (
        "<!doctype html>\n"
        f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>\n"
        f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>\n"
        f"<meta name={quote}plan-slug{quote} content={quote}{PLAN}{quote}>\n"
        f"<meta name={quote}plan-status{quote} content={quote}active{quote}>\n"
        f"<meta name={quote}plan-section-declarations{quote} content={quote}"
        f"{json.dumps(declarations).replace(quote, '&quot;')}{quote}>\n"
        f"<meta name={quote}plan-section-depends-on{quote} content={quote}"
        f"{json.dumps(CHAIN).replace(quote, '&quot;')}{quote}>\n"
        f"<h2 id={quote}s1{quote}>§1</h2>\n<h2 id={quote}s2{quote}>§2</h2>\n"
        f"<h2 id={quote}s3{quote}>§3</h2>\n"
    )


def _write_plan(repository: Path, declarations: dict[str, str]) -> None:
    (repository / "docs" / "plans" / f"{PLAN}.html").write_text(
        _plan_html(declarations), encoding="utf-8"
    )


def _declare(repository: Path, section: str) -> None:
    """Mark one section done in the plan, as its landing does."""
    path = repository / "docs" / "plans" / f"{PLAN}.html"
    text = path.read_text(encoding="utf-8")
    match = re.search(r'name="plan-section-declarations" content="([^"]*)"', text)
    assert match is not None, "the fixture always writes a declarations meta"
    raw = match.group(1)
    declarations = json.loads(raw.replace("&quot;", '"').replace("&amp;", "&"))
    declarations[section] = "done"
    path.write_text(
        text.replace(raw, json.dumps(declarations).replace('"', "&quot;"), 1),
        encoding="utf-8",
    )
    # A landing commits the plan it ticks: a dispatch refuses to run against a
    # plan that differs from the base it reads.
    _run(repository, "add", "--", str(path.relative_to(repository)))
    _run(repository, "commit", "-q", "-m", f"chore: land {section}")


@pytest.fixture()
def chain_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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
    (repository / "docs" / "plans").mkdir(parents=True)
    _write_plan(
        repository,
        {"s1": "implementable", "s2": "implementable", "s3": "implementable"},
    )
    _run(repository, "init", "-q", "-b", "main")
    _run(repository, "config", "user.email", "worker@example.invalid")
    _run(repository, "config", "user.name", "Worker")
    _run(repository, "add", "--", "docs")
    _run(repository, "add", "--", "skills")
    _run(repository, "commit", "-q", "-m", "chore: seed checkout")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _config_for():
    config = json.loads(json.dumps(CONFIG))

    def resolve(pointer):
        return config

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


def _launcher(recorded: list):
    def launch(plan, *, log_path, stderr_path, prompt_path):
        recorded.append(Path(str(prompt_path)).name)
        return os.getpid()

    return launch


def _live(repository: Path) -> dict[str, dict]:
    return {
        str(pointer["run_id"]): pointer for pointer in runs.list_live(project=PROJECT)
    }


def _phase_of(repository: Path, section: str) -> str:
    for pointer in runs.list_live(project=PROJECT):
        node = dict(pointer.get("node") or {})
        if str(node.get("section")) == section:
            return str(pointer.get("phase") or "")
    return ""


def _queue_chain(repository: Path) -> dict:
    return queue_admission.queue_requests(
        [_node("n1", "s1"), _node("n2", "s2"), _node("n3", "s3")],
        project=PROJECT,
        session=SESSION,
        repo_root=repository,
        config=CONFIG,
        run_ids={"n1": "r-n1", "n2": "r-n2", "n3": "r-n3"},
    )


def test_the_chain_queues_awaiting_upstream_and_launches_in_order(
    chain_repo: Path,
) -> None:
    stored = _queue_chain(chain_repo)
    assert stored["r-n1"]["phase"] == queue_admission.QUEUED_PHASE
    assert stored["r-n2"]["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE
    assert stored["r-n2"]["waits"] == [f"{PLAN}#s1"]
    assert stored["r-n3"]["waits"] == [f"{PLAN}#s2"]

    launched: list[str] = []
    queue_admission.admit_once(config_for=_config_for(), launcher=_launcher(launched))
    assert len(launched) == 1, "only the head of the chain has no unmet wait"
    assert _phase_of(chain_repo, "s2") == "awaiting-upstream"
    assert _phase_of(chain_repo, "s3") == "awaiting-upstream"

    _declare(chain_repo, "s1")
    queue_admission.admit_once(config_for=_config_for(), launcher=_launcher(launched))
    assert len(launched) == 2, "the middle section launches as its upstream lands"
    assert _phase_of(chain_repo, "s3") == "awaiting-upstream"

    _declare(chain_repo, "s2")
    queue_admission.admit_once(config_for=_config_for(), launcher=_launcher(launched))
    assert len(launched) == 3, "the tail launches as its upstream lands"


def test_an_abandoned_upstream_blocks_the_tail_and_never_launches(
    chain_repo: Path,
) -> None:
    stored = _queue_chain(chain_repo)
    assert stored["r-n2"]["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE

    # The head's run is closed as failed: nothing will ever deliver s1, so the
    # middle section is blocked rather than waiting, and the tail behind it is
    # blocked with it.
    def fail_head(pointer: dict) -> dict:
        pointer["phase"] = "failed"
        return pointer

    runs._mutate_pointer("r-n1", fail_head)

    launched: list[str] = []
    queue_admission.admit_once(config_for=_config_for(), launcher=_launcher(launched))

    assert launched == [], "a blocked chain launches nothing"
    assert _phase_of(chain_repo, "s2") == queue_admission.BLOCKED_UPSTREAM_PHASE
    assert _phase_of(chain_repo, "s3") == queue_admission.BLOCKED_UPSTREAM_PHASE


def test_a_malformed_node_is_refused_at_queue_time(chain_repo: Path) -> None:
    malformed = _node("n2", "s2", inputs=["a-file-the-repository-does-not-hold.csv"])
    with pytest.raises(CrewError) as refusal:
        queue_admission.queue_requests(
            [_node("n1", "s1"), malformed],
            project=PROJECT,
            session=SESSION,
            repo_root=chain_repo,
            config=CONFIG,
        )
    assert "refused at queue time" in str(refusal.value)
    sections = {
        str(dict(pointer.get("node") or {}).get("section"))
        for pointer in runs.list_live(project=PROJECT)
    }
    assert "s2" not in sections, "the malformed node wrote no request"
