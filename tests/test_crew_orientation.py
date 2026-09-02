from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import crew


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "crew-home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _pointer(crew_home: Path, *, phase: str = "working") -> dict:
    run_id = "orientation-run"
    manifest = crew_home / "runs" / run_id / "manifest.md"
    record = {
        "run_id": run_id,
        "project": "proj",
        "backend": "in-harness",
        "launch": "in-harness",
        "task": "task-orientation",
        "phase": phase,
        "worktree": "/repo/worktrees/orientation-run",
        "base_sha": "a" * 40,
        "node": {
            "id": "orientation-node",
            "write_paths": ["reckon/crew/dispatch.py", "tests/test_orientation.py"],
        },
        "manifest_path": str(manifest),
        "manifest_baseline_mtime_ns": 0,
    }
    crew._write_json(crew.pointer_path(run_id), record)
    return record


def _report(record: dict, **overrides: object) -> str:
    values = {
        "orientation_worktree": record["worktree"],
        "orientation_base_sha": record["base_sha"],
        "orientation_write_paths": json.dumps(record["node"]["write_paths"]),
        **overrides,
    }
    return "".join(f"{name}: {value}\n" for name, value in values.items())


def test_composed_prompt_requires_orientation_as_the_first_manifest_write() -> None:
    node = crew.TaskNode(
        id="orientation-node",
        goal="verify worker orientation",
        plan="plan-a",
        section="s7",
        role="implement",
        done_when="three orientation facts are checked on first observation",
        write_paths=["reckon/crew/dispatch.py", "tests/test_orientation.py"],
        time_budget="20m",
    )

    prompt = crew.compose_prompt(
        node=node,
        project="proj",
        worktree="/repo/worktrees/orientation-run",
        working_directory="/repo/worktrees/orientation-run",
        manifest_path="/state/runs/orientation-run/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
    )

    worktree = prompt.index("orientation_worktree:")
    revision = prompt.index("orientation_base_sha:")
    scope = prompt.index("orientation_write_paths:")
    node_key = prompt.index("  node: orientation-node")
    assert worktree < revision < scope < node_key
    assert "first three lines your first write" in prompt
    assert (
        'orientation_write_paths: ["reckon/crew/dispatch.py","tests/test_orientation.py"]'
        in prompt
    )


def test_first_observation_blocks_and_names_a_mismatched_field(
    crew_home: Path,
) -> None:
    record = _pointer(crew_home)
    manifest = Path(record["manifest_path"])
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        _report(record, orientation_worktree="/repo/the-wrong-worktree")
    )

    observed = crew.observe(record["run_id"], config={})

    assert observed["phase"] == "blocked"
    assert observed["orientation_check"]["mismatches"] == ["worktree"]
    assert "worktree" in observed["detail"]
    assert record["worktree"] in observed["detail"]
    assert "/repo/the-wrong-worktree" in observed["detail"]


def test_first_observation_leaves_a_matching_phase_untouched(crew_home: Path) -> None:
    record = _pointer(crew_home, phase="working")
    manifest = Path(record["manifest_path"])
    manifest.parent.mkdir(parents=True)
    manifest.write_text(_report(record))

    observed = crew.observe(record["run_id"], config={})

    assert observed["phase"] == "working"
    assert observed["orientation_check"]["matched"] is True
    assert "detail" not in observed


def test_first_observation_does_not_treat_an_absent_report_as_a_mismatch(
    crew_home: Path,
) -> None:
    record = _pointer(crew_home, phase="starting")

    observed = crew.observe(record["run_id"], config={})

    assert observed["phase"] == "starting"
    assert "orientation_check" not in observed
    assert "detail" not in observed
