"""A batch entry takes every field a single dispatch takes.

Queuing a coordinator's chosen sections in one beat must not force a separate
single dispatch for the fields that path accepts. An entry carries an over-fence
reason, a steering comment, the local-lane choice and a queue priority, and
stores them on the same request a single dispatch stores. A command-level
option given beside ``--batch`` applies to every entry that does not set it. A
dry-run batch validates every entry, prints one row for each and writes nothing
into the crew home. A compound duration such as ``1h30m`` is the ninety minutes
it says, wherever a duration is accepted.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import dispatch_batch, queue_admission, queue_order, runs
from reckon.crew.node import CrewError, parse_duration
from reckon.crew.runs import crew_home

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
                "s3": _section("Three", "three.py"),
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


def _queue(repository: Path, entries: list[dict], **options) -> dict:
    def config_for(_project: str) -> dict:
        return json.loads(json.dumps(CONFIG))

    return dispatch_batch.queue_batch(
        entries,
        session=SESSION,
        config_for=config_for,
        repo_root=repository,
        **options,
    )


def _entry(node: str, section: str, **fields) -> dict:
    entry = {
        "project": PROJECT,
        "plan": PLAN,
        "section": section,
        "node": node,
        "goal": f"run {node}",
        "time_budget": "20m",
        "spec_level": "exact",
    }
    entry.update(fields)
    return entry


def _pointer_for(section: str) -> dict:
    for pointer in runs.list_live(project=PROJECT):
        node = dict(pointer.get("node") or {})
        if node.get("plan") == PLAN and node.get("section") in {
            section,
            "dispatch",
        }:
            return pointer
    raise AssertionError(f"no queued request for {PLAN}#{section}")


def _pointer_for_node(node_id: str) -> dict:
    for pointer in runs.list_live(project=PROJECT):
        if str((pointer.get("node") or {}).get("id") or "") == node_id:
            return pointer
    raise AssertionError(f"no queued request for node {node_id}")


def _home_snapshot() -> dict[str, tuple[int, int]]:
    home = crew_home()
    if not home.exists():
        return {}
    return {
        str(path.relative_to(home)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(home.rglob("*"))
        if path.is_file()
    }


def _gate_hold(pointer: dict) -> str | None:
    """The admission gate's verdict: why it holds, or ``None`` to admit."""
    return queue_admission._plan_review_owed(
        pointer, config_for=lambda _pointer: ENFORCING
    )


def test_an_entry_carries_the_four_fields_a_single_dispatch_carries(
    batch_repo: Path,
) -> None:
    result = _queue(
        batch_repo,
        [
            _entry(
                "carried-one",
                "s1",
                over_fence_reason="the measured gate predates the fence",
                comment="steer this one",
                local=True,
                priority=5,
            )
        ],
    )
    assert result["ok"] is True, json.dumps(result["results"], indent=2)
    pointer = _pointer_for_node("carried-one")

    # Each field lands where a single dispatch stores it, so a reader of the
    # stored request cannot tell which route queued it.
    assert pointer["node"]["over_fence_reason"] == (
        "the measured gate predates the fence"
    )
    assert pointer["dispatch_options"]["comment"] == "steer this one"
    assert pointer["local"] is True
    assert pointer["backend"] == CONFIG["local_backend"]
    assert pointer["priority_override"] == 5
    assert pointer["priority_override_set_at"]
    assert pointer["priority_override_reason"]


def test_a_command_level_option_reaches_every_entry(batch_repo: Path) -> None:
    result = _queue(
        batch_repo,
        [
            _entry("plain-one", "s1"),
            _entry("own-comment", "s2", comment="mine"),
            _entry("own-reason", "s3", over_fence_reason="mine as well"),
        ],
        over_fence_reason="the resolved fence predates the gate",
        comment="command-level steering",
        local=True,
    )
    assert result["ok"] is True, json.dumps(result["results"], indent=2)

    plain = _pointer_for_node("plain-one")
    assert plain["node"]["over_fence_reason"] == (
        "the resolved fence predates the gate"
    )
    assert plain["dispatch_options"]["comment"] == "command-level steering"
    assert plain["local"] is True

    own_comment = _pointer_for_node("own-comment")
    assert own_comment["dispatch_options"]["comment"] == "mine"
    assert own_comment["node"]["over_fence_reason"] == (
        "the resolved fence predates the gate"
    )

    own_reason = _pointer_for_node("own-reason")
    assert own_reason["node"]["over_fence_reason"] == "mine as well"
    assert own_reason["dispatch_options"]["comment"] == "command-level steering"


def test_a_dry_run_batch_prints_a_row_per_entry_and_writes_nothing(
    batch_repo: Path,
) -> None:
    before = _home_snapshot()

    result = _queue(
        batch_repo,
        [
            _entry("dry-one", "s1"),
            # Refused: a refused entry still prints its own row.
            _entry("dry-two", "s2", goal=""),
            _entry("dry-three", "s3", priority=5, local=True, comment="dry"),
        ],
        dry_run=True,
    )
    after = _home_snapshot()

    assert result["dry_run"] is True
    assert result["queued"] == 0
    assert result["would_queue"] == 2
    assert [row["index"] for row in result["results"]] == [0, 1, 2]
    assert [row["status"] for row in result["results"]] == [
        "would-queue",
        "refused",
        "would-queue",
    ]
    assert after == before
    assert list(runs.list_live(project=PROJECT)) == []


def test_the_dry_run_flag_accepts_a_batch(
    batch_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from reckon import cli, crew_dispatch_commands
    from tests import test_dispatch_names_its_backend as backend_tests

    monkeypatch.setattr(
        crew_dispatch_commands,
        "_dispatch_resolved_flight",
        lambda *_a, **_k: json.loads(json.dumps(CONFIG)),
    )
    batch = tmp_path / "batch.json"
    batch.write_text(
        json.dumps([_entry("cli-one", "s1"), _entry("cli-two", "s2")]),
        encoding="utf-8",
    )
    before = _home_snapshot()

    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "dispatch",
            "--batch",
            str(batch),
            "--session",
            SESSION,
            "--repo",
            str(batch_repo),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = backend_tests._payload(result)
    assert payload["dry_run"] is True
    assert [row["status"] for row in payload["results"]] == [
        "would-queue",
        "would-queue",
    ]
    assert payload["queued"] == 0
    assert _home_snapshot() == before
    assert list(runs.list_live(project=PROJECT)) == []


def test_a_prioritised_entry_is_admitted_before_its_unranked_sibling(
    batch_repo: Path,
) -> None:
    result = _queue(
        batch_repo,
        [_entry("unranked-one", "s1"), _entry("ranked-one", "s2", priority=5)],
    )
    assert result["ok"] is True, json.dumps(result["results"], indent=2)
    ranked = _pointer_for_node("ranked-one")
    assert ranked["priority_override"] == 5
    assert ranked["priority_override_set_at"]
    assert ranked["priority_override_reason"]
    assert "priority_override" not in _pointer_for_node("unranked-one")

    queued = [
        pointer
        for pointer in runs.list_live(project=PROJECT)
        if str(pointer.get("phase") or "") == "queued"
    ]
    order = queue_order.admission_order(
        queued, runs.list_live(project=PROJECT), now=datetime.now(tz=UTC)
    )
    assert str(order[0]["node"]["id"]) == "ranked-one", [
        row["node"]["id"] for row in order
    ]


def test_a_compound_duration_is_the_minutes_it_says() -> None:
    assert parse_duration("1h30m") == 90 * 60
    assert parse_duration("90m") == 90 * 60
    assert parse_duration("1h30m15s") == 90 * 60 + 15
    assert parse_duration("25m") == 25 * 60
    with pytest.raises(CrewError):
        parse_duration("1h30")


def test_a_compound_duration_queues(batch_repo: Path) -> None:
    result = _queue(
        batch_repo,
        [
            _entry(
                "long-one",
                "s1",
                time_budget="1h30m",
                over_fence_reason="the measured gate takes ninety minutes",
            )
        ],
    )
    assert result["ok"] is True, json.dumps(result["results"], indent=2)
    pointer = _pointer_for_node("long-one")
    assert pointer["node"]["time_budget"] == "1h30m"
    assert parse_duration(pointer["node"]["time_budget"]) == 90 * 60


def test_a_mistyped_lane_or_priority_is_refused_naming_it(batch_repo: Path) -> None:
    result = _queue(
        batch_repo,
        [
            _entry("string-local", "s1", local="false"),
            _entry("string-priority", "s2", priority="5"),
        ],
    )
    rows = {row["node"]: row for row in result["results"]}
    assert result["ok"] is False
    assert rows["string-local"]["status"] == "refused"
    assert "local" in rows["string-local"]["reason"]
    assert rows["string-priority"]["status"] == "refused"
    assert "priority" in rows["string-priority"]["reason"]
    assert list(runs.list_live(project=PROJECT)) == []
