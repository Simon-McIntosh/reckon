"""Only launches that register the anchored server replace the built-in edits."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from reckon import _backends
from reckon._flight_schema import BackendConfig
from reckon.anchored_edit import AnchoredEditor
from reckon.crew import dispatch_launch, runs
from reckon.crew.dispatch_admission import _fence_write_roots

BASE_SETTINGS = (
    '{\n  "enableAllProjectMcpServers": false,\n'
    '  "enabledMcpjsonServers": [],\n  "hooks": {\n    "Stop": []\n  }\n}\n'
)
BASE_MCP = (
    '{\n  "mcpServers": {\n    "operator": {\n'
    '      "command": "operator-server"\n    }\n  }\n}\n'
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _inbox_group() -> dict:
    """The PostToolUse group seeding adds, written out rather than built.

    Composing this through the installer would assert the function under test
    against itself: a change to the script name or the interpreter layout would
    move both sides together and the case would still pass. The literal states
    what a run home must contain — the checkout's interpreter running the hook
    script from this repository — so the seeding must agree with it.
    """
    script = REPO_ROOT / "reckon" / "hooks" / "worker_inbox.py"
    interpreter = REPO_ROOT / ".venv" / "bin" / "python"
    return {"hooks": [{"type": "command", "command": f"{interpreter} {script}"}]}


def _with_inbox_hook(document: dict, *, sorted_keys: bool = False) -> bytes:
    """The settings bytes a run home holds once the inbox hook is merged in.

    Seeding keeps every key and hook group the document already carries and adds
    exactly one — the run's inbox delivery hook, without which the run has no
    channel at all. The expectation is written out in full rather than as a
    containment check, so a case still fails if any other byte changes.
    ``sorted_keys`` states which writer had the last word: the MCP-settings seed
    rewrites a home it created with its keys sorted, while a merge into a file
    the run already wrote leaves the order it found.
    """
    merged = dict(document)
    merged["hooks"] = {
        **(merged.get("hooks") or {}),
        "PostToolUse": [_inbox_group()],
    }
    return (json.dumps(merged, indent=2, sort_keys=sorted_keys) + "\n").encode()


@pytest.fixture
def launch_case(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(tmp_path / "state"))
    home = tmp_path / "operator"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text('{"hooks":{"Stop":[]}}')
    (home / ".claude.json").write_text(
        '{"mcpServers":{"operator":{"command":"operator-server"}}}'
    )
    run = runs.run_dir("synthetic")
    run.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(_backends.tempfile, "gettempdir", lambda: str(scratch))
    roots = _fence_write_roots(
        backend={"sandbox": "worktree-full"},
        repository=tmp_path / "repository",
        run_directory=run,
        manifest_path=run / "manifest.md",
        worktree=worktree,
        declared_write_paths=["source.py"],
    )
    assert worktree not in roots
    assert not any(worktree.is_relative_to(root) for root in roots)
    monkeypatch.setattr(
        _backends, "fence_argv", lambda argv, **kwargs: ["fence", *argv]
    )
    monkeypatch.setattr(_backends, "seed_write_lock_namespace", lambda home: None)

    def compose(command="claude", enabled=True, **overrides):
        options = {
            "backend_name": "synthetic",
            "backend": {
                "launch": "cli",
                "command": command,
                "sandbox": "worktree-full",
                **({"anchored_edit": enabled} if enabled is not None else {}),
            },
            "prompt": "node prompt",
            "worktree": worktree,
            "manifest_path": run / "manifest.md",
            "writable_directories": roots,
            "fence_home": home,
            "fence_config": {},
        }
        options.update(overrides)
        return _backends.launch_plan(**options)

    compose.write_roots = roots
    return compose, home, run, worktree


def _spec(plan, run, worktree, monkeypatch):
    monkeypatch.setattr(
        dispatch_launch,
        "_persisted_worker_environment",
        lambda environment, **kwargs: dict(environment),
    )
    return dispatch_launch._supervisor_spec(
        run_id=run.name,
        run_directory=run,
        repo_root=worktree,
        worktree=worktree,
        plan=plan,
        fenced=plan.argv[0] == "fence",
        prompt_path=run / "prompt.txt",
        log_path=run / "stream.jsonl",
        stderr_path=run / "stderr.log",
    )


def _assert_receipt(plan, run, worktree, monkeypatch, enabled, reason):
    assert plan.anchored_edit is enabled
    assert plan.anchored_edit_reason == reason
    assert plan.as_dict()["anchored_edit"] is enabled
    spec = _spec(plan, run, worktree, monkeypatch)
    assert spec["anchored_edit"] is enabled
    assert spec["anchored_edit_reason"] == reason
    payload = dispatch_launch._worker_record_payload(
        spec, pid=0, attempt=1, launched_at="", registered_at=""
    )
    assert payload["anchored_edit"] is enabled
    assert payload["anchored_edit_reason"] == reason
    return spec


@pytest.mark.parametrize("command", ["claude", "clive"])
@pytest.mark.parametrize("enabled", [False, None])
def test_key_false_preserves_base_argv_and_settings_bytes(
    launch_case, command, enabled
):
    compose, _, run, worktree = launch_case
    plan = compose(command=command, enabled=enabled)
    expected = [
        "fence",
        command,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--add-dir",
        str(worktree),
        *(str(root) for root in _backends.fence_writable_roots(compose.write_roots)),
    ]
    assert json.dumps(plan.argv).encode() == json.dumps(expected).encode()
    assert (run / "harness" / "settings.json").read_bytes() == _with_inbox_hook(
        json.loads(BASE_SETTINGS), sorted_keys=True
    )
    assert (run / "harness" / ".claude.json").read_bytes() == BASE_MCP.encode()
    assert plan.anchored_edit is False


@pytest.mark.parametrize("command", ["claude", "clive"])
def test_fresh_fenced_launch_registers_scoped_tools_and_records_treatment(
    launch_case, monkeypatch, command
):
    compose, home, run, worktree = launch_case
    plan = compose(command=command)
    config = json.loads((run / "harness" / ".claude.json").read_text())
    server = config["mcpServers"]["reckon-anchored-edit"]
    assert server["command"] == sys.executable
    expected_roots = _backends.fence_writable_roots((*compose.write_roots, worktree))
    assert server["args"] == [
        "-m",
        "reckon.anchored_edit",
        *(word for root in expected_roots for word in ("--root", str(root))),
    ]
    assert config["mcpServers"]["operator"] == {"command": "operator-server"}
    settings = json.loads((run / "harness" / "settings.json").read_text())
    assert settings["permissions"]["allow"] == [
        "mcp__reckon-anchored-edit__read_anchored",
        "mcp__reckon-anchored-edit__edit_anchored",
    ]
    index = plan.argv.index("--disallowedTools")
    assert plan.argv[index + 1 : index + 3] == ["Edit", "MultiEdit"]
    assert "Write" not in plan.argv
    _assert_receipt(plan, run, worktree, monkeypatch, True, None)
    assert (home / ".claude" / "settings.json").read_text() == '{"hooks":{"Stop":[]}}'
    assert json.loads((home / ".claude.json").read_text())["mcpServers"] == {
        "operator": {"command": "operator-server"}
    }


@pytest.mark.parametrize("command", ["claude", "clive"])
def test_a_seeding_launch_instructs_the_anchored_tools(launch_case, command):
    """The prompt a treated launch hands over names both tools and their route.

    The instruction rides on the launch that seeded the server, so it is
    read from the plan's own prompt rather than from the base signal prompt:
    a dispatch-time line keyed on the flight key would also reach a launch
    that seeded nothing.
    """
    compose, _, _, _ = launch_case
    plan = compose(command=command)
    assert plan.anchored_edit is True
    assert plan.prompt.startswith("node prompt")
    assert "read_anchored" in plan.prompt
    assert "edit_anchored" in plan.prompt
    # The harness defers MCP tools, so the worker must be told how to load them.
    assert "ToolSearch" in plan.prompt
    assert (
        "select:mcp__reckon-anchored-edit__read_anchored,"
        "mcp__reckon-anchored-edit__edit_anchored" in plan.prompt
    )


def test_a_prompt_already_instructed_keeps_one_copy(launch_case):
    """A lane change composes from the prompt file, which already carries one.

    The launch that follows appends its own instruction, so the block present
    is removed first: the worker is told once, and the rest of the prompt is
    left as it was written.
    """
    compose, _, _, _ = launch_case
    from reckon.crew.prompts import (
        ANCHORED_EDIT_CLAUDE_INSTRUCTION,
        ANCHORED_EDIT_HEADING,
    )

    already = (
        f"node prompt\n\n{ANCHORED_EDIT_HEADING}\n{ANCHORED_EDIT_CLAUDE_INSTRUCTION}\n"
    )
    plan = compose(prompt=already)
    assert plan.anchored_edit is True
    assert plan.prompt.count(ANCHORED_EDIT_HEADING) == 1
    assert plan.prompt.startswith("node prompt\n\n")
    assert plan.prompt.rstrip().endswith(ANCHORED_EDIT_CLAUDE_INSTRUCTION)


@pytest.mark.parametrize(
    "reason", ["unfenced", "resumed", "operator-settings", "no-flight-config"]
)
@pytest.mark.parametrize("command", ["claude", "clive"])
def test_a_launch_seeding_no_server_carries_no_instruction(
    launch_case, reason, command
):
    """A launch with no anchored server is never told to use its tools."""
    compose, _, run, _ = launch_case
    options = {}
    if reason == "unfenced":
        options["fence"] = False
    elif reason == "resumed":
        options["resume_session"] = "recorded-session"
    elif reason == "no-flight-config":
        options["fence_config"] = None
    else:
        (run / "harness").mkdir()
        (run / "harness" / "settings.json").write_text('{"operator":true}')
    plan = compose(command=command, **options)
    assert plan.anchored_edit is False
    assert plan.prompt == "node prompt"


@pytest.mark.parametrize(
    "reason", ["unfenced", "resumed", "operator-settings", "no-flight-config"]
)
@pytest.mark.parametrize("command", ["claude", "clive"])
def test_ineligible_launch_keeps_builtin_tools_and_records_why(
    launch_case, monkeypatch, reason, command
):
    compose, _, run, worktree = launch_case
    options = {}
    if reason == "unfenced":
        options["fence"] = False
    elif reason == "resumed":
        options["resume_session"] = "recorded-session"
    elif reason == "no-flight-config":
        options["fence_config"] = None
    else:
        (run / "harness").mkdir()
        (run / "harness" / "settings.json").write_text('{"operator":true}')
    plan = compose(command=command, **options)
    assert "--disallowedTools" not in plan.argv
    _assert_receipt(plan, run, worktree, monkeypatch, False, reason)
    config_path = run / "harness" / ".claude.json"
    if config_path.exists():
        assert "reckon-anchored-edit" not in json.loads(config_path.read_text()).get(
            "mcpServers", {}
        )
    if reason == "operator-settings":
        assert (run / "harness" / "settings.json").read_bytes() == _with_inbox_hook(
            {"operator": True}
        )


def test_a_treated_run_resumes_with_builtin_tools_and_a_false_receipt(
    launch_case, monkeypatch
):
    compose, _, run, worktree = launch_case
    assert compose().anchored_edit is True
    settings = (run / "harness" / "settings.json").read_bytes()
    plan = compose(resume_session="recorded-session")
    assert "--disallowedTools" not in plan.argv
    assert (run / "harness" / "settings.json").read_bytes() == settings
    _assert_receipt(plan, run, worktree, monkeypatch, False, "resumed")


@pytest.mark.parametrize("enabled", [True, False])
def test_launch_fact_reaches_the_run_pointer(launch_case, monkeypatch, enabled):
    compose, _, run, worktree = launch_case
    plan = compose(enabled=enabled)
    spec = _assert_receipt(
        plan, run, worktree, monkeypatch, enabled, None if enabled else "disabled"
    )
    pointer = runs.pointer_path(run.name)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(
        json.dumps({"run_id": run.name, "phase": "starting", "attempt": 1})
    )
    dispatch_launch._publish_stored_phase(spec, ended=False)
    recorded = runs.read_pointer(run.name)
    assert recorded["anchored_edit"] is enabled
    assert recorded["anchored_edit_reason"] == (None if enabled else "disabled")


def test_old_attempt_cannot_replace_the_treatment_receipt(launch_case, monkeypatch):
    compose, _, run, worktree = launch_case
    spec = _spec(compose(), run, worktree, monkeypatch)
    pointer = runs.pointer_path(run.name)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(
        json.dumps(
            {
                "run_id": run.name,
                "attempt": 2,
                "phase": "working",
                "anchored_edit": False,
                "anchored_edit_reason": "resumed",
            }
        )
    )
    dispatch_launch._publish_stored_phase(spec, ended=False)
    assert runs.read_pointer(run.name)["anchored_edit"] is False
    assert runs.read_pointer(run.name)["anchored_edit_reason"] == "resumed"


def test_backend_schema_accepts_the_key_and_json_schema_agrees():
    for enabled in (False, True):
        parsed = BackendConfig.model_validate(
            {"name": "synthetic", "anchored_edit": enabled}
        )
        assert parsed.anchored_edit is enabled
    assert not BackendConfig.model_validate({"name": "synthetic"}).anchored_edit
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "docs" / "_shared" / "flight.schema.json").read_text())
    assert schema["$defs"]["BackendConfig"]["properties"]["anchored_edit"]["type"] == [
        "boolean",
        "null",
    ]


def test_the_registered_server_command_can_start_without_a_console_script(launch_case):
    compose, _, run, _ = launch_case
    compose()
    config = json.loads((run / "harness" / ".claude.json").read_text())
    server = config["mcpServers"]["reckon-anchored-edit"]
    result = subprocess.run(
        [server["command"], *server["args"], "--help"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--root" in result.stdout


@pytest.mark.parametrize("command", ["claude", "clive"])
def test_production_roots_allow_source_edits_inside_the_worktree(launch_case, command):
    compose, home, run, worktree = launch_case
    source = worktree / "source.py"
    source.write_text("original value\n")
    plan = compose(command=command)
    assert plan.anchored_edit is True
    assert "--disallowedTools" in plan.argv
    config = json.loads((run / "harness" / ".claude.json").read_text())
    args = config["mcpServers"]["reckon-anchored-edit"]["args"]
    roots = [args[index + 1] for index, value in enumerate(args) if value == "--root"]
    editor = AnchoredEditor(roots)
    rendered = editor.read(str(source))
    assert not rendered.startswith("refused:"), rendered
    assert rendered.endswith("|original value")
    anchor = rendered.split("|", 1)[0]
    result = editor.edit(
        str(source), [{"op": "replace", "anchor": anchor, "text": "edited value"}]
    )
    assert not result.startswith("refused:"), result
    assert source.read_text() == "edited value\n"
    outside = home / "outside.py"
    outside.write_text("outside value\n")
    refusal = editor.read(str(outside))
    assert refusal.startswith("refused:"), refusal
    assert "outside every --root" in refusal
