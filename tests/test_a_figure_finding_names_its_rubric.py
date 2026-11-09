"""A figure finding's dispose command names its rubric.

A run is reviewed under more than one rubric and each family is stored beside
the others, so the verb selects a figure record only when the command carries
``--rubric figure``. The command an obligation hands a coordinator for a
blocking finding is composed in one place from the stored record, and while
that composer knew the record's review run but not its family it printed the
text-review form for every finding. The coordinator ran the command the
obligation gave, and it was refused for selecting no record at all, so the
finding could not be answered from the surface that raised it.

Every crew directory resolves inside a temporary configuration home and every
store under a temporary tree, so nothing touches the operator's own stores.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew import review as review_module
from reckon.crew import runs

PROJECT = "figure-rubric-dispatch-fixture"
SESSION = "coordinator-fixture"
FIGURE_RUN = "r-20261020T120000000000-figure-finding-run"
TEXT_RUN = "r-20261020T120100000000-text-finding-run"
FIGURE_REVIEW_RUN = "r-20261020T140000000000-figure-review-run"
TEXT_REVIEW_RUN = "r-20261020T140100000000-text-review-run"
FOLD_NODE = "fix-a"
OBSERVED_AT = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)

# Index 1 is the blocking finding on each record: it is what refuses the next
# review round, and so what the duty is built from.
FIGURE_FINDINGS: list[dict[str, Any]] = [
    {
        "file": "docs/figures/p/p.png",
        "line": "1",
        "text": "the axis label is clipped at the canvas edge",
        "severity": "follow-on",
    },
    {
        "file": "docs/figures/p/p.png",
        "line": "2",
        "text": "the caption restates the title",
        "severity": review_module.BLOCKING_FINDING_SEVERITY,
    },
]
TEXT_FINDINGS: list[dict[str, Any]] = [
    {
        "file": "reckon/crew/thing.py",
        "line": "10",
        "text": "the guard never fires",
        "severity": "follow-on",
    },
    {
        "file": "reckon/crew/thing.py",
        "line": "20",
        "text": "the refusal names the wrong field",
        "severity": review_module.BLOCKING_FINDING_SEVERITY,
    },
]


def _git(tree: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=tree,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@dataclass(frozen=True)
class Fixture:
    """The temporary world one assertion reads its report from."""

    repository: Path
    head_sha: str
    figure_record_path: Path
    text_record_path: Path


def _record(
    run_id: str,
    *,
    review_run_id: str,
    head_sha: str,
    findings: list[dict[str, Any]],
    rubric: str | None = None,
) -> dict[str, Any]:
    """One run-review record in the store's own shape, or a figure record."""
    record: dict[str, Any] = {
        "project": PROJECT,
        "reviewed_run_id": run_id,
        "review_run_id": review_run_id,
        "status": "parsed",
        "reviewed_base_sha": head_sha,
        "reviewed_head_sha": head_sha,
        "findings": findings,
    }
    if rubric is not None:
        record["rubric"] = rubric
    return record


def _seed_checkout(tmp_path: Path) -> tuple[Path, str]:
    """A git checkout under the temp tree, and its one commit's revision."""
    repository = tmp_path / "repo"
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True)
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the fixture checkout"),
    ):
        _git(repository, *arguments)
    return repository, _git(repository, "rev-parse", "HEAD")


def _write_pointer(run_id: str, *, worktree: Path, head_sha: str) -> dict[str, Any]:
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "process_alive": False,
        "repo": str(worktree),
        "worktree": str(worktree),
        "node": {
            "id": run_id,
            "plan": "",
            "section": "",
            "time_budget": "20m",
            "write_paths": ["docs"],
        },
        "review_dispatch": {
            "status": "refused",
            "reason": (
                f"run {run_id!r} cannot be reviewed: its newest review carries "
                "1 blocking finding(s) with no disposition"
            ),
            "run_id": None,
            "head": head_sha,
            "at": OBSERVED_AT.isoformat(),
        },
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _mount(config_home: Path, repository: Path) -> None:
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )


@pytest.fixture()
def fixture(tmp_path: Path, isolated_reckon_home: Path) -> Fixture:
    """A temporary crew home holding one figure review and one text review."""
    repository, head_sha = _seed_checkout(tmp_path)
    _mount(isolated_reckon_home, repository)
    _write_pointer(FIGURE_RUN, worktree=repository, head_sha=head_sha)
    _write_pointer(TEXT_RUN, worktree=repository, head_sha=head_sha)

    figure_path = review_module.store_review(
        _record(
            FIGURE_RUN,
            review_run_id=FIGURE_REVIEW_RUN,
            head_sha=head_sha,
            findings=FIGURE_FINDINGS,
            rubric=review_module.FIGURE_REVIEW_RUBRIC,
        )
    )
    text_path = review_module.store_review(
        _record(
            TEXT_RUN,
            review_run_id=TEXT_REVIEW_RUN,
            head_sha=head_sha,
            findings=TEXT_FINDINGS,
        )
    )
    assert figure_path != text_path
    return Fixture(
        repository=repository,
        head_sha=head_sha,
        figure_record_path=figure_path,
        text_record_path=text_path,
    )


def _duty(fixture: Fixture, run_id: str):
    """The duty the obligation hands a coordinator for one held review."""
    from reckon.crew import obligations as obligations_module

    pointer = runs.read_pointer(run_id)
    return obligations_module._review_dispatch_refusal(
        PROJECT,
        pointer,
        row={
            "kind": "review-missing",
            "run_id": run_id,
            "node": f"{run_id}-node",
            "age_seconds": 600,
            "next_command": f"reckon crew resume --run {run_id}",
        },
        head=fixture.head_sha,
        now=OBSERVED_AT,
    )


def _argv(command: str) -> list[str]:
    """The argv a printed command runs, with its placeholder node named."""
    filled = command.replace("<the node the finding went into>", FOLD_NODE)
    assert "<" not in filled, filled
    words = shlex.split(filled)
    assert words[:2] == ["reckon", "crew"], words
    return words[2:]


def _run(command: str):
    return CliRunner().invoke(cli_main, ["crew", *_argv(command)])


def _figure_flags(record: dict[str, Any]) -> dict[str, Any] | None:
    dispositions = record.get(review_module.FINDING_DISPOSITIONS_KEY)
    if not isinstance(dispositions, dict):
        return None
    entry = dispositions.get("1")
    return entry if isinstance(entry, dict) else None


def test_a_figure_finding_gets_a_command_that_names_its_rubric(
    fixture: Fixture,
) -> None:
    """The duty's command names the figure review and its rubric, and it runs."""
    duty = _duty(fixture, FIGURE_RUN)
    assert duty is not None, "no duty was raised for the held figure review"
    command = str(duty["next_command"])

    # The printed fold is the command that answers the finding, so running it
    # must land the disposition on the figure record. It is run before its
    # spelling is asserted, so a composer that drops the rubric fails here as
    # the refusal an operator would meet rather than on a string comparison.
    # The text record is read back unchanged, because a command that wrote the
    # text record would be indistinguishable from a correct one at every later
    # read.
    commands = command.split("`")[1::2]
    assert len(commands) == 2, command
    result = _run(commands[0])
    assert result.exit_code == 0, result.output
    for printed in commands:
        assert "--rubric figure" in printed, printed
    assert f"--review {FIGURE_REVIEW_RUN}" in command, command
    assert "--finding 1" in command, command
    receipt = json.loads(result.output)
    assert receipt["path"] == str(fixture.figure_record_path)
    assert receipt["finding_index"] == 1

    recorded = _figure_flags(
        json.loads(fixture.figure_record_path.read_text(encoding="utf-8"))
    )
    assert recorded is not None
    assert recorded["kind"] == "folded"
    assert recorded["node"] == FOLD_NODE
    text_stored = json.loads(fixture.text_record_path.read_text(encoding="utf-8"))
    assert review_module.FINDING_DISPOSITIONS_KEY not in text_stored


def test_a_text_finding_keeps_the_command_it_had(fixture: Fixture) -> None:
    """A text review's command carries no rubric flag, and it still runs."""
    duty = _duty(fixture, TEXT_RUN)
    assert duty is not None, "no duty was raised for the held text review"
    command = str(duty["next_command"])

    assert "--rubric" not in command, command
    assert f"--review {TEXT_REVIEW_RUN}" in command, command

    commands = command.split("`")[1::2]
    assert len(commands) == 2, command
    assert all("--rubric" not in printed for printed in commands), command
    result = _run(commands[0])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["path"] == str(fixture.text_record_path)

    text_stored = json.loads(fixture.text_record_path.read_text(encoding="utf-8"))
    assert review_module.FINDING_DISPOSITIONS_KEY in text_stored
