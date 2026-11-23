"""A whole-plan dispatch takes the options a batch file takes, a dry run included.

``crew dispatch --project P --plan SLUG`` with no ``--section`` queues every
implementable section of the plan. A coordinator previews it with ``--dry-run``
before queueing, and carries ``--comment``, ``--local`` and
``--over-fence`` to every section, exactly as beside ``--batch``.
"""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew_dispatch_commands


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def record(batch_path: Any, **kwargs: Any) -> None:
        calls.append({"batch_path": batch_path, **kwargs})

    monkeypatch.setattr(crew_dispatch_commands, "_queue_dispatch_batch", record)
    return calls


def _dispatch(*extra: str) -> Any:
    return CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "dispatch",
            "--project",
            "proj",
            "--plan",
            "plan-a",
            "--session",
            "coordinator-a",
            *extra,
        ],
    )


def test_a_whole_plan_dry_run_reaches_the_plan_queue(
    queued: list[dict[str, Any]],
) -> None:
    result = _dispatch("--dry-run")
    assert result.exit_code == 0, result.output
    assert len(queued) == 1, queued
    call = queued[0]
    assert call["batch_path"] is None
    assert call["plan"] == "plan-a"
    assert call["project"] == "proj"
    assert call["dry_run"] is True


def test_a_whole_plan_dispatch_carries_the_command_options(
    queued: list[dict[str, Any]],
) -> None:
    result = _dispatch("--comment", "queued whole", "--over-fence", "measured long run")
    assert result.exit_code == 0, result.output
    call = queued[0]
    assert call["dry_run"] is False
    assert call["comment"] == "queued whole"
    assert call["over_fence_reason"] == "measured long run"


def test_naming_a_section_keeps_the_single_node_path(
    queued: list[dict[str, Any]],
) -> None:
    _dispatch("--section", "s1", "--dry-run")
    assert queued == []
