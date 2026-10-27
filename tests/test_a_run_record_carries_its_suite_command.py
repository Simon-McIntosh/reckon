"""A dispatch records the suite command its promotion will actually run.

The flight file may arm a project with a command carrying the scoped
placeholder, which promotion substitutes with the node's own fenced test files.
Dispatch used to store that declaration verbatim, so the run record carried a
string nobody could run and the worker was told no command at all: it learned
which suite its promotion re-runs only when a promotion refused it.

Dispatch now resolves the declaration through promotion's own substitution,
records the resolved command beside the template it came from, and states it in
the composed prompt. A scope holding no test file resolves to nothing, and the
record says the gate is unarmed for that node rather than carrying an empty
command.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon import flight as flight_module

BACKENDS = {
    "default_backend": "worker",
    "backends": {
        "worker": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "20m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}

SCOPED_TEMPLATE = "uv run pytest {node_test_files} -q"
PLACEHOLDER = "{node_test_files}"


@pytest.fixture()
def project_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    """A throwaway repository, a crew home, and a flight file arming its gate.

    The suite declaration is read from the flight file rather than written into
    the test's config dict, so the case exercises the declaration a project
    actually carries.
    """
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    root = tmp_path / "repo"
    (root / "skills" / "reckon-build" / "scripts").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
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
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)

    flight_file = tmp_path / "flight.yaml"
    flight_file.write_text(
        "gates:\n  suite_command: " + json.dumps(SCOPED_TEMPLATE) + "\n",
        encoding="utf-8",
    )
    brief = tmp_path / "brief.md"
    brief.write_text(
        "Record the measured bound for one node so a later reader can compare it.\n",
        encoding="utf-8",
    )
    return root, flight_file, brief


def _config(flight_file: Path) -> dict:
    """The config's own gate layer, resolved from the flight file on disk."""
    absent = flight_file.parent / "absent-host.yaml"
    resolved = flight_module.resolve(
        host_path=absent, project_path=flight_file, shipped_path=absent
    )
    config = dict(BACKENDS)
    config["gates"] = dict(resolved.config.get("gates") or {})
    return config


def _node(
    node_id: str, *, write_paths: list[str], manifests: Path, brief: Path
) -> crew.TaskNode:
    return crew.TaskNode(
        id=node_id,
        brief=str(brief),
        goal="record the measured bound for one node",
        plan="",
        spec_level="exact",
        role="implement",
        done_when="uv run pytest reports the measured bound",
        negative_control="store the bound unrounded in a scratch extraction",
        write_paths=write_paths,
        time_budget="20m",
        manifest_path=str(manifests / f"{node_id}.md"),
    )


def _dispatch(
    root: Path,
    flight_file: Path,
    brief: Path,
    *,
    node_id: str,
    write_paths: list[str],
) -> dict:
    return crew.dispatch(
        node=_node(
            node_id, write_paths=write_paths, manifests=root.parent, brief=brief
        ),
        project="proj",
        repo=root,
        config=_config(flight_file),
        session=f"session-{node_id}",
        launcher=lambda *args, **kwargs: os.getpid(),
    )


def test_a_dispatch_resolves_the_scoped_command_for_the_node(
    project_tree: tuple[Path, Path, Path],
) -> None:
    root, flight_file, brief = project_tree

    record = _dispatch(
        root,
        flight_file,
        brief,
        node_id="suite-armed",
        write_paths=["tests/test_node_measure.py", "reckon/measure.py"],
    )

    expected = "uv run pytest tests/test_node_measure.py -q"
    assert record["suite_gate"] == "armed"
    # The template is kept beside the resolved command, so a reader can tell a
    # scoped declaration from the command it produced.
    assert record["suite_command"] == SCOPED_TEMPLATE
    assert record["suite_command_resolved"] == expected
    assert PLACEHOLDER not in record["suite_command_resolved"]

    prompt = Path(record["prompt_path"]).read_text(encoding="utf-8")
    assert expected in prompt
    assert PLACEHOLDER not in prompt


def test_a_scope_with_no_test_file_records_an_unarmed_gate(
    project_tree: tuple[Path, Path, Path],
) -> None:
    root, flight_file, brief = project_tree

    record = _dispatch(
        root,
        flight_file,
        brief,
        node_id="suite-unarmed",
        write_paths=["reckon/measure.py"],
    )

    assert record["suite_gate"] == "unarmed"
    assert record["suite_command_resolved"] is None
    # The declaration itself is kept: the project is armed, this scope is not.
    assert record["suite_command"] == SCOPED_TEMPLATE

    prompt = Path(record["prompt_path"]).read_text(encoding="utf-8")
    assert PLACEHOLDER not in prompt
    assert "unarmed for this node" in prompt
