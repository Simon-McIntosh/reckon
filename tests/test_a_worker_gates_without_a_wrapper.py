"""A dispatched worker follows its gate calls without creating a script."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import crew
from reckon.cli import main
from reckon.crew import prompts, standing_suite
from reckon.crew.promotion_records import _arm_missing_fields, _evaluate_suite_delta
from reckon.crew.reports import audit_manifest
from reckon.crew.runs import read_pointer


def _git(root, *arguments):
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def dispatched(tmp_path, monkeypatch):
    real_root = os.environ.get("RECKON_GATE_SMOKE_ROOT")
    parent = Path(tempfile.mkdtemp(dir=real_root)) if real_root else tmp_path
    home = parent / "home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_WORKTREE_ROOT", str(parent / "worktrees"))
    root = parent / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests/test_kept.py").write_text("def test_kept():\n    assert True\n")
    fleet = Path("skills/reckon-build/scripts/worktree_fleet.py")
    (root / fleet).parent.mkdir(parents=True)
    shutil.copyfile(Path(__file__).parents[1] / fleet, root / fleet)
    (root / ".venv").symlink_to(Path(sys.executable).parent.parent)
    (root / ".gitignore").write_text(".venv\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "tests/test_kept.py", str(fleet), ".gitignore")
    _git(
        root,
        "commit",
        "-qm",
        "test: seed project",
        "-m",
        "Provide an isolated gate population.",
    )
    brief = parent / "brief.md"
    brief.write_text("Measure the node's scoped tests on all_debug.\n")
    delivery = home / "crew/reports/scoped/manifest.md"
    node = crew.TaskNode(
        id="worker-gate",
        plan="",
        brief=str(brief),
        role="test",
        goal="measure the node's scoped tests",
        spec_level="exact",
        done_when="The scoped tests pass on all_debug.",
        negative_control="withhold the composed calls from the prompt",
        write_paths=[str(delivery)],
        time_budget="20m",
        manifest_path=str(delivery),
    )
    template = f"PYTHONPATH=. {shlex.quote(sys.executable)} -m pytest -p no:cacheprovider -q tests/test_kept.py"
    config = {
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
        "roles": {"test": {}},
        "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
        "gates": {"suite_command": template},
    }
    if os.environ.get("RECKON_TEST_WITHHOLD_GATE_COMMANDS"):
        monkeypatch.setattr(prompts, "worker_gate_commands", lambda **kwargs: "")
    record = crew.dispatch(
        node=node,
        project="fixture",
        repo=root,
        config=config,
        session="fixture-session",
        launcher=lambda *args, **kwargs: os.getpid(),
    )
    tree = Path(record["worktree"])
    _git(tree, "config", "user.email", "test@example.invalid")
    _git(tree, "config", "user.name", "Test")
    (tree / "tests/test_added.py").write_text("def test_added():\n    assert True\n")
    _git(tree, "add", "tests/test_added.py")
    _git(
        tree,
        "commit",
        "-qm",
        "test: extend population",
        "-m",
        "Measure a file present only at the head.",
    )
    yield record, node, tree
    _git(root, "worktree", "remove", str(tree))


@pytest.fixture
def scheduler(tmp_path, monkeypatch):
    """Exercise the submitted shell while keeping unit tests off the scheduler."""
    calls = []
    if os.environ.get("RECKON_GATE_SMOKE_ROOT"):
        yield calls
        return
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "srun"
    shim.write_text('#!/bin/sh\nwhile [ "$1" != bash ]; do shift; done\nexec "$@"\n')
    shim.chmod(0o700)
    real_run = subprocess.run

    def submit(argv, **options):
        if argv[0] != "sbatch":
            return real_run(argv, **options)
        calls.append(argv)
        assert "--partition=all_debug" in argv
        assert "--wait" in argv
        assert not any(key.startswith("SLURM_") for key in options["env"])
        environment = dict(options["env"])
        environment["PATH"] = str(bin_dir) + os.pathsep + environment["PATH"]
        return real_run(
            ["bash", "-c", argv[-1]],
            cwd=options["cwd"],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=options["timeout"],
        )

    monkeypatch.setattr(standing_suite, "subprocess", SimpleNamespace(run=submit))
    yield calls


def _calls(record):
    prompt = Path(record["prompt_path"]).read_text()
    calls = [
        line.strip()
        for line in prompt.splitlines()
        if line.strip().startswith("PYTHONPATH=")
        and "from reckon.cli import main; main()" in line
    ]
    assert len(calls) == 2, (
        "wrapper-free assertion: prompt must carry both executable calls"
    )
    return calls


def _invoke(command):
    tokens = shlex.split(command)
    return CliRunner().invoke(main, tokens[tokens.index("crew") :])


def test_dispatched_worker_follows_composed_calls(dispatched, scheduler):
    record, node, tree = dispatched
    gate_call, arms_call = _calls(record)
    gate_result = _invoke(gate_call)
    assert gate_result.exit_code == 0, gate_result.output
    gate = json.loads(gate_result.output)
    arms_result = _invoke(arms_call)
    assert arms_result.exit_code == 0, arms_result.output
    arms = json.loads(arms_result.output)
    base, head = arms["baseline_suite"], arms["after_suite"]
    pointer = read_pointer(record["run_id"])
    assert base["revision"] == pointer["base_sha"]
    assert head["revision"] == _git(tree, "rev-parse", "HEAD")
    assert base["command"] == head["command"]
    for field, arm in (("baseline_suite", base), ("after_suite", head)):
        assert not _arm_missing_fields(pointer, field, arm, head=head["revision"])
        assert arm["completed"] and arm["failure_count"] == 0
        assert arm["partition"] == "all_debug"
        text = Path(arm["log_path"]).read_text()
        assert arm["revision"] in text and arm["command"] in text
        assert text.rstrip().endswith("EXIT=0")
    run_directory = Path(record["prompt_path"]).parent
    assert not list(run_directory.glob("*.sh"))
    assert not list(run_directory.glob("*.py"))
    manifest = {
        "node": node.id,
        "status": "complete",
        "commits": [head["revision"]],
        "changed_paths": [],
        "tests": gate["tests"],
        "test_logs": gate["test_logs"],
        "baseline_suite": base,
        "after_suite": head,
        "landing": "Scoped gate and both arms passed.",
        "messages_received": "none",
    }
    text = "\n".join(
        f"{key}: {json.dumps(value) if not isinstance(value, str) else value}"
        for key, value in manifest.items()
    )
    checked = audit_manifest(
        text, node, worktree=tree, repository=tree, project="fixture"
    )
    assert not checked["findings"], checked
    delivery = Path(node.manifest_path)
    delivery.parent.mkdir(parents=True, exist_ok=True)
    delivery.write_text(text)
    delta = _evaluate_suite_delta(
        record["run_id"], pointer, waiver_reason="", dry_run=True
    )
    assert delta["status"] == "clean", delta
    if real_root := os.environ.get("RECKON_GATE_SMOKE_ROOT"):
        Path(real_root, "receipt.json").write_text(
            json.dumps(
                {
                    "scoped_gate": gate,
                    "arms": arms,
                    "promotion_delta": delta,
                    "manifest_path": str(delivery),
                    "prompt_path": record["prompt_path"],
                    "wrapper_scripts": [],
                },
                indent=2,
            )
        )
    if not os.environ.get("RECKON_GATE_SMOKE_ROOT"):
        assert len(scheduler) == 3


def test_suite_resolves_each_tree_without_carrying_added_tests(dispatched, scheduler):
    record, _, tree = dispatched
    pointer = read_pointer(record["run_id"])
    pointer["suite_command"] = pointer["suite_command"].replace(
        "tests/test_kept.py", "{node_test_files}"
    )
    pointer["node"] = {"write_paths": ["tests/test_kept.py", "tests/test_added.py"]}
    result = standing_suite.run_node_arms(
        pointer=pointer,
        repository=tree,
        run_id=record["run_id"],
        partition="all_debug",
        timeout_seconds=60,
    )
    assert result["ok"], result
    base, head = result["baseline_suite"], result["after_suite"]
    assert "tests/test_added.py" not in base["command"]
    assert "tests/test_added.py" in head["command"]
    assert "1 passed" in Path(base["log_path"]).read_text()
    assert "2 passed" in Path(head["log_path"]).read_text()


def test_custom_scoped_command_failure_returns_its_receipt(dispatched, scheduler):
    record, _, _ = dispatched
    gate_call, _ = _calls(record)
    result = _invoke(
        gate_call + " --command " + shlex.quote("printf 'scope-marker\\n'; exit 7")
    )
    assert result.exit_code != 0
    report = json.loads(result.output)
    assert not report["ok"]
    assert report["gate"]["exit_status"] == 7
    log = Path(report["test_logs"][0]).read_text()
    assert "scope-marker\n" in log
    assert log.rstrip().endswith("EXIT=7")


def test_worker_gate_rejects_another_checkout(dispatched, scheduler, tmp_path):
    record, _, _ = dispatched
    gate_call, _ = _calls(record)
    arguments = shlex.split(gate_call)
    arguments[arguments.index("--checkout-path") + 1] = str(tmp_path)
    result = _invoke(shlex.join(arguments))
    assert result.exit_code != 0
    assert "does not own checkout" in result.output
    assert not scheduler


def test_worker_gate_receipt_is_unreadable_when_submission_never_runs(
    dispatched, monkeypatch
):
    record, _, _ = dispatched

    def submit(argv, **options):
        if argv[0] == "sbatch":
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.run(argv, check=options.pop("check", False), **options)

    monkeypatch.setattr(standing_suite, "subprocess", SimpleNamespace(run=submit))
    gate_call, _ = _calls(record)
    result = _invoke(gate_call)
    assert result.exit_code != 0
    assert not json.loads(result.output)["gate"]["completed"]


@pytest.mark.parametrize("verb", [("verify-gate",), ("suite", "run")])
def test_worker_gate_verbs_offer_no_caller_log_option(verb):
    result = CliRunner().invoke(main, ["crew", *verb, "--help"])
    assert result.exit_code == 0
    assert "--log" not in result.output
    assert "--partition" in result.output
    refused = CliRunner().invoke(main, ["crew", *verb, "--log-path", "sentinel"])
    assert refused.exit_code == 2
    assert "No such option" in refused.output and "--log-path" in refused.output
