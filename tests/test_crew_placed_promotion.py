"""A placed run promotes on the identity its record names, not on argv[0].

The defect was found by promoting a placed run rather than by its tests. A
placed launch prefixes the scheduler onto the argv that was already resolved,
so the first word of a run record's argv names the scheduler while the lane the
run was launched on sits beside it. A promotion reads that record back to
decide which translation speaks its stream, and a reader taking the harness
from argv[0] finds the scheduler there:

    no launch translation for command 'srun'; reckon can translate: claude,
    clive, codex

so the run cannot be promoted at all, though its launch was correct and its
prefixed argv is the faithful record of what ran. The launch cases could not
see it: they exercised the plan that is built, not the record that is later
read.

These cases are that round trip. The record is promoted through the same path
``reckon crew complete`` takes, with a stream on disk, so the translation runs
rather than being described.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest

from reckon import _backends, _plan_html, crew, ledger
from reckon.crew.runs import _write_json, pointer_path

# ``reckon.crew`` resolves to a function rather than the package, so the
# module a run's record is read with is reached by name.
dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = "proj"
PLAN = "plan-a"

# The argv a placed launch records: the scheduler first, then the resolved
# harness, exactly as the prefix leaves them.
SCHEDULER_PREFIX = ["/usr/bin/srun", "--partition=all"]
HARNESS_EXECUTABLE = "/opt/backends/bin/clive"
PLACED_ARGV = [
    *SCHEDULER_PREFIX,
    HARNESS_EXECUTABLE,
    "-p",
    "--output-format",
    "stream-json",
    "--verbose",
]


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git worktree with one mounted plan, which a promotion writes into."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Plan A",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _write_stream(path: Path) -> Path:
    """A finished turn's stream, so the promotion reaches the translation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {"type": "system", "subtype": "init", "session_id": "s-placed"},
        {
            "type": "result",
            "subtype": "success",
            "terminal_reason": "end_turn",
            "is_error": False,
            "num_turns": 1,
            "duration_api_ms": 1000,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            "result": "done",
            "timestamp": "2026-09-20T00:00:00Z",
        },
    ]
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    return path


def _placed_record(repository: Path, run_id: str, stream: Path) -> dict:
    """A placed run as dispatch records it.

    The lane is named by ``backend``, the harness by the explicit ``command``
    and the resolved translation by ``dialect``, all recorded beside the argv
    the launch actually ran.
    """
    return {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(repository),
        "launch": "cli",
        "role": "implement",
        "backend": "clive",
        "command": HARNESS_EXECUTABLE,
        "dialect": "claude",
        "argv": list(PLACED_ARGV),
        "log_path": str(stream),
        "created_at": "2026-09-20T00:00:00Z",
        "manifest_path": "/durable/manifest.md",
        "node": {
            "id": "placed-promotion",
            "plan": PLAN,
            "section": "placed-promotion",
            "time_budget": "25m",
            "write_paths": [],
        },
    }


def test_a_placed_run_promotes_on_its_declared_backend(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "r-20260920T000000000001-placed"
    stream = _write_stream(tmp_path / "stream.jsonl")
    record = _placed_record(repository, run_id, stream)
    _write_json(pointer_path(run_id), record)

    seen: list[dict] = []
    observe = _backends.observe_log

    def spy(**kwargs):  # type: ignore[no-untyped-def]
        seen.append(dict(kwargs.get("backend") or {}))
        return observe(**kwargs)

    monkeypatch.setattr(_backends, "observe_log", spy)
    crew.complete(run_id, gate="passed", root=repository)

    # The premise: the record's first word is the scheduler, so a reader that
    # takes the harness from it is reading the wrong lane's name and is
    # refused rather than answered.
    assert Path(record["argv"][0]).name == "srun"
    with pytest.raises(_backends.BackendError):
        _backends.dialect_for({"command": record["argv"][0]})

    # The claim: the promotion read the stream through the harness the record
    # declares, and that translation resolved the lane rather than the stem of
    # argv[0]. Asserted on the identity actually handed to the reader, so a
    # repair that resolved the lane by some other route is not mistaken for it.
    assert seen, "the promotion never reached the run's stream"
    declaring = [
        mapping
        for mapping in seen
        if str(mapping.get("command") or "") == HARNESS_EXECUTABLE
    ]
    assert declaring, (
        "no reader on the promotion path used the harness the record declares; "
        f"the identities read were {seen}"
    )
    assert _backends.dialect_for(declaring[0]).name == "claude"
    assert not any(
        Path(str(mapping.get("command") or "")).name == "srun" for mapping in declaring
    )

    # And it landed: the promotion reached its durable home and released the
    # live pointer, rather than resolving the translation and stopping short.
    row = ledger.load(PROJECT, repository)[0]["runs"][0]
    assert row["run_id"] == run_id
    assert row["gate"] == "passed"
    assert not pointer_path(run_id).exists()


def test_a_record_naming_only_its_lane_still_resolves_it() -> None:
    """A placed record written before the explicit fields existed.

    Its argv names the scheduler and nothing else identifies the harness, so
    the lane is the only identity it carries. The translation consults it
    rather than refusing, which is what makes an in-flight placed run
    promotable after the reader is repaired.
    """
    legacy = {
        "run_id": "r-legacy",
        "launch": "cli",
        "backend": "clive",
        "argv": list(PLACED_ARGV),
        "log_path": "/nonexistent/stream.jsonl",
    }

    settings = dispatch_module._backend_settings(legacy, None)
    assert Path(str(settings.get("command"))).name == "srun"
    assert _backends.dialect_for(settings).name == "claude"


def test_a_record_naming_no_translatable_lane_is_still_refused() -> None:
    """The repair widens the identity, not the refusal.

    A backend whose name is free-form user data — a lane called ``spark`` —
    and whose argv names only the scheduler has no identity reckon can
    translate, and it must be refused rather than guessed at.
    """
    opaque = {
        "run_id": "r-opaque",
        "launch": "cli",
        "backend": "spark",
        "argv": list(PLACED_ARGV),
        "log_path": "/nonexistent/stream.jsonl",
    }

    settings = dispatch_module._backend_settings(opaque, None)
    with pytest.raises(_backends.BackendError) as refused:
        _backends.dialect_for(settings)
    assert "srun" in str(refused.value)


def test_a_config_backend_naming_no_translatable_command_is_still_refused() -> None:
    """Dispatch strictness is unchanged: a config mapping carries no identity.

    The identity the translation consults is recorded on a run, so a backend
    mapping built from configuration — the shape dispatch validates — is
    refused exactly as before when its command names no dialect.
    """
    with pytest.raises(_backends.BackendError) as refused:
        _backends.dialect_for({"launch": "cli", "command": "/usr/bin/srun"})
    assert "srun" in str(refused.value)
    assert "clive" in str(refused.value)
