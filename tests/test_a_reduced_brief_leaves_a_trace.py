"""A brief cut after a context refusal leaves a trace on the run and its row.

A coordinator whose node is refused for context fit shortens the brief and
dispatches the same node again. That cut is what made the node fit, and until
this record existed nothing said so: the accepted run looked exactly like one
whose brief had always been that small, on the live pointer and on the ledger
row promotion writes from it. So a refusal writes what it measured under the
crew home, the next dispatch of that node that fits carries the reduction on
its node block, and a first-attempt run carries no marker at all.

The estimate is measured through routing's own verdict function here, with the
standing instruction chain pinned so the two attempts differ only in the brief
the test wrote.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import routing

PROJECT = "project-a"
PLAN = f"{PROJECT}-plan"
SECTION = "dispatch"
NODE_ID = "reduced-brief"
SESSION = "a-reduced-brief-leaves-a-trace"

# The refusal trace is found by the directory it is written into rather than by
# importing the module that writes it, so the cases that assert its absence also
# run against a tree that does not have the module at all.
REFUSAL_TRACES = "refusal-traces"

STANDING_INSTRUCTION_TOKENS = 20_000
USABLE_INPUT_WINDOW = 30_000
LARGE_FILE_BYTES = 700_000

CONFIG = {
    "default_backend": "worker",
    "backends": {
        "worker": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "20m",
            "usable_input_window": USABLE_INPUT_WINDOW,
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setattr(
        routing,
        "_standing_context_input",
        lambda *_args, **_kwargs: (
            STANDING_INSTRUCTION_TOKENS,
            {"effective_tokens": STANDING_INSTRUCTION_TOKENS},
        ),
    )
    return config_home


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture()
def repository(tmp_path: Path, home: Path) -> Path:
    root = tmp_path / "repo"
    (root / "skills" / "reckon-build" / "scripts").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "package").mkdir()
    fleet_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (root / "skills" / "reckon-build" / "scripts" / "worktree_fleet.py").write_text(
        fleet_script.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (root / "docs" / "plans" / f"{PLAN}.html").write_text(
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{PLAN}">'
        f'<h2 id="{SECTION}">{SECTION}</h2>',
        encoding="utf-8",
    )
    (root / "package" / "target.py").write_text("value = 1\n", encoding="utf-8")
    (root / "package" / "payload.py").write_text(
        "x" * LARGE_FILE_BYTES, encoding="utf-8"
    )
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs", "package", "skills"),
        ("commit", "-q", "-m", "chore: seed fixture"),
    ):
        _git(root, *arguments)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _node(*, goal: str, done_when: str, write_paths: list[str]) -> crew.TaskNode:
    return crew.TaskNode(
        id=NODE_ID,
        goal=goal,
        plan=PLAN,
        section=SECTION,
        spec_level="exact",
        done_when=done_when,
        write_paths=list(write_paths),
        time_budget="20m",
    )


def _oversized() -> crew.TaskNode:
    return _node(
        goal="record the reduction a shortened brief carried",
        done_when="pytest reports the reduction cases passing against package/payload.py",
        write_paths=["package/payload.py"],
    )


def _shortened() -> crew.TaskNode:
    return _node(
        goal="record the reduction a cut brief carried",
        done_when="pytest reports the reduction cases passing",
        write_paths=["package/target.py"],
    )


def _dispatch(node: crew.TaskNode, repository: Path) -> dict:
    return crew.dispatch(
        node=node,
        project=PROJECT,
        repo=repository,
        config=CONFIG,
        session=SESSION,
        check_budget=False,
        launcher=lambda *args, **kwargs: 0,
    )


def test_a_refused_node_that_fits_on_a_second_attempt_records_the_reduction(
    home: Path, repository: Path
) -> None:
    with pytest.raises(crew.CompetenceLimit) as refused:
        _dispatch(_oversized(), repository)

    verdict = refused.value.verdict
    assert verdict["reason"].startswith("context-window-exceeded")
    traces = sorted((home / "crew" / REFUSAL_TRACES).glob("*.json"))
    assert len(traces) == 1, f"the refusal left {len(traces)} traces, not one"
    trace = traces[0]

    record = _dispatch(_shortened(), repository)

    reduction = record["node"]["brief_reduction"]
    assert reduction["retried_after_refusal"] is True
    assert reduction["refused_estimated_tokens"] == verdict["estimated_tokens"]
    assert (
        reduction["accepted_estimated_tokens"] < reduction["refused_estimated_tokens"]
    )
    assert reduction["refused_window_tokens"] == verdict["window_tokens"]
    assert reduction["removed_paths"] == ["package/payload.py"]
    assert reduction["refused_at"]
    # The trace answered a question about the run that carried it, so the next
    # dispatch of this node cannot inherit an older reduction.
    assert not trace.exists()


def test_the_reduction_survives_promotion_onto_the_ledger_row(
    home: Path, repository: Path
) -> None:
    with pytest.raises(crew.CompetenceLimit):
        _dispatch(_oversized(), repository)
    record = _dispatch(_shortened(), repository)
    reduction = record["node"]["brief_reduction"]

    crew.complete(record["run_id"], gate="passed", root=repository)

    row = ledger.load(PROJECT, repository)[0]["runs"][0]
    assert row["run_id"] == record["run_id"]
    assert row["node_definition"]["brief_reduction"] == reduction


def test_a_first_attempt_carries_no_reduction_marker(
    home: Path, repository: Path
) -> None:
    record = _dispatch(_shortened(), repository)

    assert "brief_reduction" not in record["node"]
    assert not (home / "crew" / REFUSAL_TRACES).exists()


def test_a_refusal_is_never_written_into_the_repository(
    home: Path, repository: Path
) -> None:
    before = _git(repository, "status", "--porcelain")
    with pytest.raises(crew.CompetenceLimit):
        _dispatch(_oversized(), repository)

    assert _git(repository, "status", "--porcelain") == before
    assert sorted(repository.rglob("*refusal*")) == []
