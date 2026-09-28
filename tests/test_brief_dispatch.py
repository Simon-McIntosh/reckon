"""Dispatch takes a stored brief in place of a committed plan section.

A brief names no plan, so the committed-section and plan-review gates, which
both join the node to a plan blob and a stored review, must be skipped for it
without loosening them for a plan dispatch. The brief is durable authority: the
run keeps its own copy, and the pointer names the digest and the stored path so
a later reader can open the exact bytes the worker read. The refusals a plan
dispatch carries — a missing done-when, a stated second authority — apply to a
brief dispatch unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew
from reckon.crew import runs

CONFIG = {
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
        },
        "alternate": {
            "launch": "cli",
            "command": "codex",
            "model": "another-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "20m",
        },
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}

BRIEF_TEXT = (
    "Measure the paid lane's queue shape and record the bound you compared it\n"
    "against, so the next reader can tell the measurement from a guess.\n"
)


@pytest.fixture()
def brief_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
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
    (config_home / "mounts.json").write_text(
        json.dumps({"proj": str(root / "docs")}), encoding="utf-8"
    )

    brief = tmp_path / "brief.md"
    brief.write_text(BRIEF_TEXT, encoding="utf-8")
    return config_home, root, brief


def _brief_node(config_home: Path, *, done_when: str, node_id: str) -> crew.TaskNode:
    return crew.TaskNode(
        id=node_id,
        goal="measure the paid lane queue shape",
        plan="",
        brief="",
        spec_level="exact",
        role="implement",
        done_when=done_when,
        write_paths=["result.json"],
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / f"{node_id}.md"),
    )


def _cli_arguments(
    repo: Path,
    brief: Path,
    *,
    node_id: str,
    done_when: str | None,
    plan: str | None = None,
    section: str | None = None,
    dry_run: bool,
) -> list[str]:
    arguments = [
        "crew",
        "dispatch",
        "--project",
        "proj",
        "--brief",
        str(brief),
    ]
    if plan is not None:
        arguments += ["--plan", plan]
    if section is not None:
        arguments += ["--section", section]
    arguments += [
        "--role",
        "implement",
        "--spec-level",
        "exact",
        "--node",
        node_id,
        "--goal",
        "measure the paid lane queue shape",
        "--write-path",
        "result.json",
        "--time-budget",
        "20m",
        "--session",
        f"session-{node_id}",
        "--repo",
        str(repo),
    ]
    if done_when is not None:
        arguments += ["--done-when", done_when]
    arguments.append("--dry-run" if dry_run else "--no-watch")
    return arguments


def _invoke(repo: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str]):
    monkeypatch.setattr(cli_module, "_resolved_flight", lambda *_a, **_k: CONFIG)
    monkeypatch.setattr(
        cli_module, "_model_availability_refusal", lambda *_a, **_k: None
    )
    return CliRunner().invoke(cli_module.main, arguments)


def _payload(result) -> dict:
    return json.loads(result.output.splitlines()[0])


DONE_WHEN = "the gate command pytest exits 0 and prints the measured bound"


def test_a_brief_dispatch_dry_run_resolves_without_a_plan(
    brief_repo: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _config_home, repo, brief = brief_repo
    result = _invoke(
        repo,
        monkeypatch,
        _cli_arguments(
            repo, brief, node_id="brief-dry", done_when=DONE_WHEN, dry_run=True
        ),
    )

    payload = _payload(result)

    assert result.exit_code == 0, result.output
    assert payload["ok"] is True
    # The dry run reaches the same digest a launch would record, so a
    # validating caller can see the brief it is about to send.
    assert (
        payload["brief"]["sha256"]
        == hashlib.sha256(BRIEF_TEXT.encode("utf-8")).hexdigest()
    )
    assert payload["node"]["brief"] == str(brief)


def test_a_brief_dispatch_records_its_digest_and_copy_on_the_pointer(
    brief_repo: tuple[Path, Path, Path],
) -> None:
    _config_home, repo, brief = brief_repo
    node = _brief_node(_config_home, done_when=DONE_WHEN, node_id="brief-live")
    node.brief = str(brief)

    record = crew.dispatch(
        node=node,
        project="proj",
        repo=repo,
        config=CONFIG,
        session="session-brief-live",
        launcher=lambda *args, **kwargs: os.getpid(),
    )

    digest = hashlib.sha256(BRIEF_TEXT.encode("utf-8")).hexdigest()
    assert record["brief"]["sha256"] == digest
    stored = Path(record["brief"]["path"])
    assert stored.is_relative_to(runs.run_dir(record["run_id"]))
    assert stored.read_text(encoding="utf-8") == BRIEF_TEXT
    assert record["brief"]["source_path"] == str(brief)

    # The pointer on disk carries the same block, so a reader who never sees
    # the returned record still reaches the exact bytes the worker read.
    pointer = json.loads(runs.pointer_path(record["run_id"]).read_text())
    assert pointer["brief"]["sha256"] == digest
    assert Path(pointer["brief"]["path"]).read_text(encoding="utf-8") == BRIEF_TEXT


def test_a_brief_dispatch_without_a_done_when_is_refused_as_not_dispatchable(
    brief_repo: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _config_home, repo, brief = brief_repo
    result = _invoke(
        repo,
        monkeypatch,
        _cli_arguments(
            repo, brief, node_id="brief-no-measure", done_when=None, dry_run=False
        ),
    )

    assert result.exit_code == 2, result.output
    assert _payload(result)["error"] == "not-dispatchable"


def test_a_brief_and_a_plan_together_are_refused(
    brief_repo: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _config_home, repo, brief = brief_repo
    result = _invoke(
        repo,
        monkeypatch,
        _cli_arguments(
            repo,
            brief,
            node_id="brief-and-plan",
            done_when=DONE_WHEN,
            plan="some-plan",
            dry_run=True,
        ),
    )

    assert result.exit_code != 0
    assert "--brief and --plan are mutually exclusive" in result.output


def test_a_brief_and_a_section_together_are_refused(
    brief_repo: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A section names a plan's part; with no plan it has nothing to name."""
    _config_home, repo, brief = brief_repo
    result = _invoke(
        repo,
        monkeypatch,
        _cli_arguments(
            repo,
            brief,
            node_id="brief-and-section",
            done_when=DONE_WHEN,
            section="session-routing",
            dry_run=True,
        ),
    )

    assert result.exit_code != 0
    assert "--brief and --section are mutually exclusive" in result.output


def test_a_brief_dispatch_prompt_carries_the_brief_and_no_plan_pointer(
    brief_repo: tuple[Path, Path, Path],
) -> None:
    """The brief must reach the composed prompt, not just the pointer.

    A resolved digest and a stored copy say nothing about what the worker was
    told: an empty brief passed to the composition would leave every other
    assertion green while the worker received no authority at all.
    """
    _config_home, repo, brief = brief_repo
    node = _brief_node(_config_home, done_when=DONE_WHEN, node_id="brief-prompt")
    node.brief = str(brief)

    record = crew.dispatch(
        node=node,
        project="proj",
        repo=repo,
        config=CONFIG,
        session="session-brief-prompt",
        launcher=lambda *args, **kwargs: os.getpid(),
    )

    prompt = (runs.run_dir(record["run_id"]) / "prompt.txt").read_text(encoding="utf-8")
    assert BRIEF_TEXT.strip() in prompt
    # A plan dispatch reads its authority through this pointer; a brief carries
    # the text itself, so the pointer must be absent.
    assert "PLAN     proj:" not in prompt


def test_a_later_read_survives_the_source_brief_being_removed(
    brief_repo: tuple[Path, Path, Path],
) -> None:
    """The durable copy, not the source path, serves every read after dispatch.

    A coordinator's brief is often a scratch file. Once it is gone the run must
    still resolve — a lane change rebuilds the node from the live pointer and
    re-resolves it, and reading the source path there refuses a run that is
    still perfectly dispatchable.
    """
    from reckon import ledger

    config_home, repo, brief = brief_repo
    ledger.register_member("proj", "worker-brief", harness="worker", root=repo)

    node = _brief_node(config_home, done_when=DONE_WHEN, node_id="brief-durable")
    node.brief = str(brief)
    record = crew.dispatch(
        node=node,
        project="proj",
        repo=repo,
        config=CONFIG,
        session="session-brief-durable",
        member="worker-brief",
        launcher=lambda *args, **kwargs: os.getpid(),
    )

    stored = Path(record["brief"]["path"])
    Path(record["brief"]["source_path"]).unlink()

    pointer = crew.read_pointer(record["run_id"])
    pointer.update({"phase": "working", "pid": 41001})
    crew._write_json(crew.pointer_path(record["run_id"]), pointer)

    from reckon.crew.dispatch import change_lane

    moved = change_lane(
        record["run_id"],
        "alternate",
        "the first lane is spent",
        config=CONFIG,
        launcher=lambda *args, **kwargs: 42002,
    )

    assert moved["brief"]["sha256"] == record["brief"]["sha256"]
    assert Path(moved["brief"]["path"]) == stored
