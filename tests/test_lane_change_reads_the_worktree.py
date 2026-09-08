"""A fresh lane-change session receives a measured account of its inheritance."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from reckon.crew.dispatch import _lane_prompt


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def inherited_worktree(tmp_path: Path) -> tuple[Path, Path, str]:
    worktree = tmp_path / "inherited"
    worktree.mkdir()
    tracked = worktree / "assigned.py"
    tracked.write_text("from package import original\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "assigned.py"),
        ("commit", "-q", "-m", "test: seed inherited worktree"),
    ):
        _git(worktree, *arguments)
    original_prompt = tmp_path / "original-prompt.txt"
    original_prompt.write_text("ORIGINAL WORKER PROMPT\n", encoding="utf-8")
    return worktree, original_prompt, _git(worktree, "rev-parse", "HEAD")


def _record(worktree: Path, original_prompt: Path, base: str) -> dict[str, str]:
    return {
        "run_id": "r-fixture",
        "worktree": str(worktree),
        "prompt_path": str(original_prompt),
        "base_sha": base,
    }


def test_fresh_start_names_an_uncommitted_file_and_requires_a_checkpoint(
    inherited_worktree: tuple[Path, Path, str],
) -> None:
    worktree, original_prompt, base = inherited_worktree
    (worktree / "assigned.py").write_text(
        "from package import replacement\n", encoding="utf-8"
    )

    prompt = _lane_prompt(
        _record(worktree, original_prompt, base),
        "",
        "the first lane cannot continue",
        continued=False,
    )

    assert "assigned.py" in prompt
    assert "Checkpoint instruction:" in prompt
    assert "Commit the inherited changes before continuing" in prompt
    assert "an inherited diff is the only copy" in prompt


def test_clean_worktree_is_stated_affirmatively(
    inherited_worktree: tuple[Path, Path, str],
) -> None:
    worktree, original_prompt, base = inherited_worktree

    prompt = _lane_prompt(
        _record(worktree, original_prompt, base),
        "continue carefully",
        "the first lane cannot continue",
        continued=False,
    )

    assert "Porcelain status: clean (no entries)." in prompt
    assert "Per-file change summary: no changes." in prompt


def test_advice_is_unchanged_and_distinct_from_the_reading(
    inherited_worktree: tuple[Path, Path, str],
) -> None:
    worktree, original_prompt, base = inherited_worktree
    advice = (
        "Keep  repeated  spaces; preserve <tags> & symbols.\nSecond line:\tverbatim"
    )

    prompt = _lane_prompt(
        _record(worktree, original_prompt, base),
        advice,
        "the first lane cannot continue",
        continued=False,
    )

    reading_boundary = "INHERITED WORKTREE READING (measured fact)\n"
    advice_boundary = "COORDINATOR ADVICE (instruction; passed through unchanged)\n"
    assert reading_boundary in prompt
    assert advice_boundary in prompt
    assert prompt.index(reading_boundary) < prompt.index(advice_boundary)
    assert prompt.partition(advice_boundary)[2] == advice


def test_reading_states_when_it_was_taken(
    inherited_worktree: tuple[Path, Path, str],
) -> None:
    worktree, original_prompt, base = inherited_worktree

    prompt = _lane_prompt(
        _record(worktree, original_prompt, base),
        "",
        "the first lane cannot continue",
        continued=False,
    )

    assert re.search(r"Reading taken at: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", prompt)


def test_reading_reports_head_and_a_different_recorded_base(
    inherited_worktree: tuple[Path, Path, str],
) -> None:
    worktree, original_prompt, base = inherited_worktree
    (worktree / "assigned.py").write_text(
        "from package import committed\n", encoding="utf-8"
    )
    _git(worktree, "add", "assigned.py")
    _git(worktree, "commit", "-q", "-m", "test: advance inherited worktree")
    head = _git(worktree, "rev-parse", "HEAD")

    prompt = _lane_prompt(
        _record(worktree, original_prompt, base),
        "",
        "the first lane cannot continue",
        continued=False,
    )

    assert f"Head commit: {head}" in prompt
    assert f"Recorded base: {base}" in prompt
    assert "Head differs from recorded base: yes." in prompt


def test_unreadable_worktree_is_reported_without_refusing_the_handoff(
    inherited_worktree: tuple[Path, Path, str],
) -> None:
    worktree, original_prompt, base = inherited_worktree
    missing = worktree.parent / "missing-worktree"

    prompt = _lane_prompt(
        _record(missing, original_prompt, base),
        "continue despite the missing tree",
        "the first lane cannot continue",
        continued=False,
    )

    assert "Inherited worktree could not be read." in prompt
    assert str(missing) in prompt
    assert "does not exist or is not a directory" in prompt
    assert "continue despite the missing tree" in prompt


def test_same_session_advice_is_unchanged(
    inherited_worktree: tuple[Path, Path, str],
) -> None:
    worktree, original_prompt, base = inherited_worktree
    advice = "Continue  in the same context.\nKeep this text exactly."

    prompt = _lane_prompt(
        _record(worktree, original_prompt, base),
        advice,
        "the lane changed without changing harness",
        continued=True,
    )

    assert prompt == advice
