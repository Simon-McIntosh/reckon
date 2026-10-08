"""A run's harness home seeds interpreter-bound reckon hooks.

When reckon seeds a run's harness home from the operator's ``settings.json``,
the copied ``hooks`` pass through the installer's recogniser before they are
written, so a bare ``python3 hook.py`` command a worker's shell would resolve to
the system interpreter reaches the run home bound to the checkout's own
interpreter. The recogniser rewrites only commands whose script imports reckon —
a hook that is not reckon's is copied byte-for-byte — and the operator's file is
only ever read.

Every case runs against a temporary operator home and never reads or writes the
real ``~/.claude``; the run home is built under ``tmp_path`` and the operator
file's bytes are captured before the seed and compared after.

The declared negative control makes ``_seed_harness_entry`` copy the operator's
``hooks`` verbatim again, skipping the recogniser; the bare-command case then
fails with the run home still holding a bare reckon hook script. Running this
file with ``RUN_HOME_HOOKS_MUTATION=1`` reproduces that red log.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

import pytest

from reckon import _backends
from reckon.hooks import install

NEGATIVE_CONTROL_MUTATION = (
    "copy the operator's hooks key verbatim again in _seed_harness_entry; the "
    "bare-command case fails naming a reckon hook script"
)


def _commands(document: dict) -> list[str]:
    return [
        hook["command"]
        for groups in document.get("hooks", {}).values()
        for group in groups
        for hook in group.get("hooks", [])
        if isinstance(hook, dict) and hook.get("type") == "command"
    ]


def _seed(tmp_path: Path, settings: dict) -> tuple[Path, Path]:
    """Seed a run home from a temporary ``.claude/settings.json``.

    Returns the run home and the operator's source file, so a case can prove the
    operator's bytes are unchanged after the seed.
    """
    operator = tmp_path / "operator"
    claude = operator / ".claude"
    claude.mkdir(parents=True)
    source = claude / "settings.json"
    source.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n")
    home = tmp_path / "run" / "harness"
    _backends.seed_harness_home(
        home,
        dialect_name="claude",
        operator_home=operator,
        declaration=[{"path": "settings.json", "keys": ["hooks"]}],
    )
    return home, source


@pytest.fixture(autouse=True)
def _apply_declared_mutation(monkeypatch):
    """The declared negative control: the copied hooks are written verbatim."""
    if not os.environ.get("RUN_HOME_HOOKS_MUTATION"):
        return
    monkeypatch.setattr(
        _backends, "_bind_reckon_hook_commands", lambda settings: settings
    )


def test_bare_reckon_hooks_are_bound_and_others_are_left_alone(tmp_path: Path):
    """A bare reckon hook is bound; a hook that is not reckon's is unchanged."""
    bare_stop = str(install.worker_stop_script_path())
    bare_git = str(install.worker_git_guard_script_path())
    foreign = {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "/usr/bin/foreign"}],
    }
    settings = {
        "hooks": {
            "Stop": [
                {"hooks": [{"type": "command", "command": f"python3 {bare_stop}"}]}
            ],
            "PreToolUse": [
                foreign,
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": f"python3 {bare_git}"}],
                },
            ],
        },
        "env": {"ANTHROPIC_AUTH_TOKEN": "operator-secret"},
    }
    home, source = _seed(tmp_path, settings)
    operator_bytes = source.read_bytes()

    seeded = json.loads((home / "settings.json").read_text())
    # The filtered copy carries only the hooks key, never the operator's secret.
    assert set(seeded) == {"hooks"}
    # A hook that is not reckon's keeps its exact group.
    assert seeded["hooks"]["PreToolUse"][0] == foreign

    expected_interpreter = str(install.interpreter_path())
    for name in ("worker_stop.py", "worker_git_guard.py"):
        matching = [c for c in _commands(seeded) if name in c]
        assert len(matching) == 1, f"{name}: {matching}"
        tokens = shlex.split(matching[0])
        assert tokens[0] == expected_interpreter, matching[0]
        assert Path(tokens[1]).name == name, matching[0]

    # The operator's file is only ever read.
    assert source.read_bytes() == operator_bytes


def test_a_run_home_seeded_from_a_bound_file_is_identical(tmp_path: Path):
    """Seeding from a file already holding the bound commands changes nothing."""
    bound = install.build_hook_snippet(include_git_guard=True)["hooks"]
    home, source = _seed(tmp_path, {"hooks": bound})

    seeded = (home / "settings.json").read_bytes()
    assert json.loads(seeded) == {"hooks": bound}
    assert seeded == source.read_bytes()


if __name__ == "__main__":  # pragma: no cover - reproduces the red log
    print(NEGATIVE_CONTROL_MUTATION)
