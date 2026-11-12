"""A batch entry carries the plan-review waiver a single dispatch accepts.

Queuing a coordinator's chosen sections in one beat must not force a separate
single-dispatch call for every section that needs a plan-review waiver. A batch
entry that sets ``allow_unreviewed_plan`` queues its request with the waiver
recorded on the pointer, exactly as ``--allow-unreviewed-plan`` does; admission
then admits that request instead of holding it on the review gate. An entry
without the field is queued and held at admission on the gate as before, and a
misspelt waiver field is refused by name rather than silently dropped.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon.crew import dispatch_batch, queue_admission, runs

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

#: The plan-review gate's enforced mode. Under the shipped report-only default
#: admission holds nothing, so the gate must be enforced for the unwaived entry
#: to be held and the waiver's effect to be observable.
ENFORCING = {"plan_review_gate": "enforce"}

SECTIONS = ("s1", "s2")


def _plan(slug: str, sections: dict[str, str]) -> str:
    quote = '"'
    body = "".join(
        f"<h2 id={quote}{name}{quote}>§{name[1:]}</h2>{prose}"
        for name, prose in sections.items()
    )
    declarations = dict.fromkeys(sections, "implementable")
    return (
        "<!doctype html>\n"
        f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>\n"
        f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>\n"
        f"<meta name={quote}plan-slug{quote} content={quote}{slug}{quote}>\n"
        f"<meta name={quote}plan-status{quote} content={quote}active{quote}>\n"
        f"<meta name={quote}plan-section-declarations{quote} content={quote}"
        f"{json.dumps(declarations).replace(quote, '&quot;')}{quote}>\n"
        f"{body}\n"
    )


def _section(title: str, path: str) -> str:
    return (
        f"<p>{title}.</p>"
        f"<p>Write paths: <code>{path}</code>.</p>"
        f"<p><strong>Done when</strong> the {title.lower()} check passes.</p>"
    )


def _run(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


@pytest.fixture()
def batch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout carrying one plan no review covers, under a temporary home."""
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
    (plans / f"{PLAN}.html").write_text(
        _plan(
            PLAN,
            {
                "s1": _section("One", "one.py"),
                "s2": _section("Two", "two.py"),
            },
        ),
        encoding="utf-8",
    )
    _run(repository, "init", "-q", "-b", "main")
    _run(repository, "config", "user.email", "worker@example.invalid")
    _run(repository, "config", "user.name", "Worker")
    _run(repository, "add", "--", "docs")
    _run(repository, "commit", "-q", "-m", "chore: seed checkout")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _queue(repository: Path, entries: list[dict]) -> dict:
    def config_for(_project: str) -> dict:
        return json.loads(json.dumps(CONFIG))

    return dispatch_batch.queue_batch(
        entries,
        session=SESSION,
        config_for=config_for,
        repo_root=repository,
    )


def _pointer_for(section: str) -> dict:
    for pointer in runs.list_live(project=PROJECT):
        node = dict(pointer.get("node") or {})
        if node.get("plan") == PLAN and node.get("section") == section:
            return pointer
    raise AssertionError(f"no queued request for {PLAN}#{section}")


def _gate_hold(pointer: dict) -> str | None:
    """The admission gate's verdict: why it holds, or ``None`` to admit."""
    return queue_admission._plan_review_owed(
        pointer, config_for=lambda _pointer: ENFORCING
    )


def test_a_waived_entry_queues_recording_its_waiver(batch_repo: Path) -> None:
    result = _queue(
        batch_repo,
        [
            {
                "project": PROJECT,
                "plan": PLAN,
                "section": "s1",
                "node": "waived-one",
                "goal": "build one",
                "time_budget": "20m",
                "spec_level": "exact",
                "allow_unreviewed_plan": True,
            },
            {
                "project": PROJECT,
                "plan": PLAN,
                "section": "s2",
                "node": "unwaived-two",
                "goal": "build two",
                "time_budget": "20m",
                "spec_level": "exact",
            },
        ],
    )
    rows = {row["node"]: row for row in result["results"]}
    assert result["ok"] is True, json.dumps(rows, indent=2)
    assert result["queued"] == 2
    assert rows["waived-one"]["status"] == "queued"
    assert rows["unwaived-two"]["status"] == "queued"

    waived = _pointer_for("s1")
    assert waived["dispatch_options"]["allow_unreviewed_plan"] is True
    unwaived = _pointer_for("s2")
    assert unwaived["dispatch_options"].get("allow_unreviewed_plan") is not True


def test_the_gate_holds_the_unwaived_entry_and_admits_the_waived_one(
    batch_repo: Path,
) -> None:
    _queue(
        batch_repo,
        [
            {
                "project": PROJECT,
                "plan": PLAN,
                "section": "s1",
                "node": "waived-one",
                "goal": "build one",
                "time_budget": "20m",
                "spec_level": "exact",
                "allow_unreviewed_plan": True,
            },
            {
                "project": PROJECT,
                "plan": PLAN,
                "section": "s2",
                "node": "unwaived-two",
                "goal": "build two",
                "time_budget": "20m",
                "spec_level": "exact",
            },
        ],
    )
    # The plan carries no stored review, so the gate owes one to every request
    # that does not waive it.
    held = _gate_hold(_pointer_for("s2"))
    assert held is not None, "the review gate must hold the unwaived entry"
    assert "review" in held.lower()
    # The waived entry reaches admission with nothing owed, so it launches.
    assert _gate_hold(_pointer_for("s1")) is None


def test_a_misspelt_waiver_field_is_refused_naming_it(batch_repo: Path) -> None:
    result = _queue(
        batch_repo,
        [
            {
                "project": PROJECT,
                "plan": PLAN,
                "section": "s1",
                "node": "misspelt-one",
                "goal": "build one",
                "time_budget": "20m",
                "spec_level": "exact",
                "allow_unreviewd_plan": True,
            }
        ],
    )
    assert result["ok"] is False
    row = result["results"][0]
    assert row["status"] == "refused"
    assert "allow_unreviewd_plan" in row["reason"]
    assert list(runs.list_live(project=PROJECT)) == []


def test_the_plan_is_unreviewed_so_the_gate_is_what_holds(batch_repo: Path) -> None:
    """The control the holds rest on: with no waiver and no review, the gate
    reports the plan's review is missing rather than admitting it."""
    _queue(
        batch_repo,
        [
            {
                "project": PROJECT,
                "plan": PLAN,
                "section": "s1",
                "node": "unwaived-control",
                "goal": "build one",
                "time_budget": "20m",
                "spec_level": "exact",
            }
        ],
    )
    detail = _gate_hold(_pointer_for("s1"))
    assert detail is not None
    assert "review" in detail.lower()
