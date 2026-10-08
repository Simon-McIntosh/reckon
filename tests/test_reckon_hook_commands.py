"""Check the commands written by both hook settings writers."""

from __future__ import annotations

import ast
import json
import shlex
from pathlib import Path

from click.testing import CliRunner

from reckon import cli
from reckon.hooks import install

HOOKS = Path(install.__file__).resolve().parent


def _commands(document: dict) -> list[str]:
    return [
        hook["command"]
        for groups in document.get("hooks", {}).values()
        for group in groups
        for hook in group.get("hooks", [])
        if hook.get("type") == "command"
    ]


def _importing_scripts() -> set[str]:
    scripts = set()
    for path in HOOKS.glob("*.py"):
        if path.name == "install.py":
            continue
        tree = ast.parse(path.read_text())
        if any(
            (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").split(".")[0] == "reckon"
            )
            or (
                isinstance(node, ast.Import)
                and any(alias.name.split(".")[0] == "reckon" for alias in node.names)
            )
            for node in ast.walk(tree)
        ):
            scripts.add(path.name)
    return scripts


def _sync(tmp_path: Path, settings_path: Path) -> dict:
    docs = tmp_path / "project" / "docs"
    state = docs / "state" / "sample"
    state.mkdir(parents=True, exist_ok=True)
    (state / "crew.json").write_text("{}\n")
    result = CliRunner().invoke(
        cli.main,
        [
            "sync",
            str(docs),
            "--project",
            "sample",
            "--mounts",
            str(tmp_path / "mounts.json"),
            "--state-root",
            str(tmp_path / "config-state"),
            "--claude-settings",
            str(settings_path),
            "--include-git-guard",
        ],
    )
    assert result.exit_code == 0, result.output
    return json.loads(settings_path.read_bytes())


def test_registered_reckon_importing_scripts_use_the_checkout_interpreter(
    tmp_path: Path,
) -> None:
    settings_path = tmp_path / "settings.json"
    synced = _sync(tmp_path, settings_path)
    installed = install.build_hook_snippet(include_git_guard=True)
    commands = _commands(synced) + _commands(installed)
    importing = _importing_scripts()
    registered = set()

    for command in commands:
        tokens = shlex.split(command)
        scripts = [token for token in tokens if Path(token).name in importing]
        if scripts:
            registered.update(Path(script).name for script in scripts)
            assert len(tokens) > 1 and tokens[1] in scripts, command
            assert tokens[0] == str(Path(tokens[1]).parents[2] / ".venv/bin/python"), (
                command
            )

    assert registered == importing
    for name, script in (
        ("native_agent_guard.py", cli._native_agent_guard_path()),
        ("worker_message_guard.py", cli._worker_message_guard_path()),
    ):
        matching = [
            shlex.split(command) for command in _commands(synced) if name in command
        ]
        assert matching == [[str(script)]]


def test_bare_commands_upgrade_in_place_through_both_writers(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    prompt = str(install.hook_script_path()) + " --hook prompt"
    stop = str(install.hook_script_path()) + " --hook stop"
    bare = {
        "UserPromptSubmit": [prompt],
        "SessionStart": [prompt],
        "Stop": [stop, str(install.worker_stop_script_path())],
        "PreToolUse": [str(install.worker_git_guard_script_path())],
    }
    foreign = {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "/usr/bin/foreign"}],
    }
    hooks = {
        event: [
            {"hooks": [{"type": "command", "command": command}]} for command in commands
        ]
        for event, commands in bare.items()
    }
    hooks["PreToolUse"][0]["matcher"] = "Bash"
    hooks["PreToolUse"].insert(0, foreign)
    settings_path.write_text(
        json.dumps({"hooks": hooks, "permissions": {"allow": ["Read"]}})
    )

    synced = _sync(tmp_path, settings_path)
    assert synced["hooks"]["PreToolUse"][0] == foreign
    importing = _importing_scripts()
    for event, commands in bare.items():
        observed = [
            hook["command"]
            for group in synced["hooks"][event]
            for hook in group["hooks"]
            if Path(shlex.split(hook["command"])[-1]).name in importing
            or "coordinator_obligations.py" in hook["command"]
        ]
        assert len(observed) == len(commands)
        assert all(
            shlex.split(command)[0]
            == str(Path(shlex.split(command)[1]).parents[2] / ".venv/bin/python")
            for command in observed
        )

    result = install.install_hook_settings(
        settings_path, write=True, include_git_guard=True
    )
    assert result.added == ()
    assert result.document["hooks"]["PreToolUse"][0] == foreign
    assert result.document["permissions"] == {"allow": ["Read"]}
    assert len(_commands(result.document)) == len(_commands(synced))
