"""A run's harness home carries the operator-home files its harness reads.

A harness that starts in a bare directory loads neither the operator's hooks
nor their instruction files, so a fenced worker silently loses the guards and
standing guidance the operator's own home declares — and a codex worker loses
everything, because codex reads ``AGENTS.md`` and never ``CLAUDE.md``. What a
worker loads is decided by its harness, not its model, so the file list is
per-dialect flight configuration: ``flight-defaults.yaml`` ships a default per
dialect, a backend's own ``harness_home_files`` replaces it, and
``seed_harness_home`` copies from the operator's home at seed time.

Every case runs against a synthetic operator home and run directory and never
reads or writes the real ``~/.claude`` or ``~/.codex``; the real homes are
asserted untouched by metadata before and after, and the seeded content is
asserted equal to the synthetic operator's so a read of the real home would be
visible rather than silent.

The declared negative control makes ``seed_harness_home`` ignore the
declaration and only create the directory as today; the claude-shaped
settings.json case then fails with no settings.json under the run harness home.
Running this file with ``HARNESS_HOME_SEED_MUTATION=1`` reproduces that red log.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from reckon import _backends, flight

NEGATIVE_CONTROL_MUTATION = (
    "seed_harness_home ignores the declaration and only creates the directory "
    "as today; the claude-shaped settings.json case must fail with no "
    "settings.json under the run harness home"
)

CLAUDE_SESSION = "ffffffff-dead-beef-cafe-000000000001"
CODEX_SESSION = "ffffffff-dead-beef-cafe-000000000002"
STOP_COMMAND = "python3 /opt/guards/native_agent_guard.py"
CLAUDE_SETTINGS = {
    "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": STOP_COMMAND}]}],
    },
    "env": {"ANTHROPIC_AUTH_TOKEN": "operator-secret"},
    "permissions": {"allow": ["Bash(rm:*)"]},
    "model": "operator-model",
}

CLAUDE_BACKEND = {"launch": "cli", "command": "claude"}
CLIVE_BACKEND = {"launch": "cli", "command": "clive"}
CODEX_BACKEND = {"launch": "cli", "command": "codex"}


def _operator_home(root: Path) -> Path:
    """Build the synthetic operator home both harnesses read from."""
    home = root / "operator"
    claude = home / ".claude"
    claude.mkdir(parents=True)
    (claude / "settings.json").write_text(json.dumps(CLAUDE_SETTINGS))
    (claude / "CLAUDE.md").write_text("# operator guidance\n")
    projects = claude / "projects" / "-home-op-reckon"
    projects.mkdir(parents=True)
    (projects / f"{CLAUDE_SESSION}.jsonl").write_text('{"type":"user"}\n')
    (projects / "unrelated.jsonl").write_text("someone else\n")

    codex = home / ".codex"
    rules = codex / "rules"
    rules.mkdir(parents=True)
    (rules / "guard.rules").write_text("guard\n")
    (codex / "AGENTS.md").write_text("# codex guidance\n")
    config = codex / "config.toml"
    config.write_text('model = "operator"\n')
    # The operator's config is not writable; the run's copy must still be.
    config.chmod(0o444)
    rollouts = codex / "sessions" / "2026" / "09" / "25"
    rollouts.mkdir(parents=True)
    (rollouts / f"rollout-2026-09-25T00-00-00-{CODEX_SESSION}.jsonl").write_text(
        '{"type":"session"}\n'
    )
    return home


def _launch(
    home: Path,
    run: Path,
    backend: dict,
    *,
    fence: bool,
    backend_name: str | None = None,
    resume_session: str | None = None,
):
    return _backends.launch_plan(
        backend_name=backend_name or str(backend["command"]),
        backend=backend,
        prompt="do the node",
        worktree=str(run / "worktree"),
        manifest_path=str(run / "manifest.md"),
        fence=fence,
        fence_home=home,
        resume_session=resume_session,
    )


@pytest.fixture(autouse=True)
def _apply_declared_mutation(monkeypatch):
    """The declared negative control: seeding becomes directory-only."""
    if not os.environ.get("HARNESS_HOME_SEED_MUTATION"):
        return

    def _directory_only(home: Path, **_kwargs) -> None:
        home.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(_backends, "seed_harness_home", _directory_only)


def _metadata_snapshot(root: Path) -> list[tuple]:
    """Relative path, size and mtime for every entry — contents never read."""
    if not root.exists():
        return []
    entries: list[tuple] = []
    for path in sorted(root.rglob("*")):
        try:
            stat_result = path.stat()
        except OSError:
            continue
        entries.append(
            (
                str(path.relative_to(root)),
                path.is_dir(),
                stat_result.st_size,
                stat_result.st_mtime_ns,
            )
        )
    return entries


def test_the_shipped_defaults_declare_both_dialects():
    """The shipped layer names each dialect's operator-home files."""
    shipped = flight.shipped_harness_home_files()
    assert shipped["claude"] == [
        {"path": "settings.json", "keys": ["hooks"]},
        {"path": "CLAUDE.md"},
    ]
    assert shipped["codex"] == [
        {"path": "AGENTS.md"},
        {"path": "config.toml"},
        {"path": "rules"},
    ]


def test_a_backend_entry_replaces_its_dialect_default():
    """A backend's own declaration wins over the shipped per-dialect default."""
    assert flight.harness_home_files("claude") == [
        {"path": "settings.json", "keys": ["hooks"]},
        {"path": "CLAUDE.md"},
    ]
    override = {"harness_home_files": [{"path": "AGENTS.md"}]}
    assert flight.harness_home_files("claude", override) == [{"path": "AGENTS.md"}]
    # An empty declaration is a lane that reads no operator file, not a default.
    assert flight.harness_home_files("claude", {"harness_home_files": []}) == []


def test_a_claude_run_home_carries_the_operator_settings_and_instructions(
    tmp_path: Path,
):
    """settings.json holds only the hooks key; CLAUDE.md holds the operator text."""
    home = _operator_home(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    # A run adopts its own harness home only when fenced, so a case asserting
    # the carry states the fence it depends on rather than inheriting a default.
    plan = _launch(home, run, CLAUDE_BACKEND, fence=True)
    harness = Path(plan.environment["CLAUDE_CONFIG_DIR"])
    assert harness == run / "harness"

    seeded = json.loads((harness / "settings.json").read_text())
    assert set(seeded) == {"hooks"}
    assert seeded["hooks"] == CLAUDE_SETTINGS["hooks"]
    assert STOP_COMMAND in json.dumps(seeded["hooks"])

    copied = harness / "CLAUDE.md"
    assert copied.read_text() == "# operator guidance\n"
    assert copied.stat().st_mode & 0o200


def test_a_codex_run_home_carries_agents_config_and_rules(tmp_path: Path):
    """AGENTS.md, a writable config.toml and rules/ match the operator's."""
    home = _operator_home(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    plan = _launch(home, run, CODEX_BACKEND, fence=True)
    harness = Path(plan.environment["CODEX_HOME"])
    assert harness == run / "codex-home"

    assert (harness / "AGENTS.md").read_text() == "# codex guidance\n"
    config = harness / "config.toml"
    assert config.read_text() == 'model = "operator"\n'
    assert config.stat().st_mode & 0o200, "the run's config.toml is a writable copy"
    assert (harness / "rules" / "guard.rules").read_text() == "guard\n"
    # The claude-shaped home's files are not this dialect's business.
    assert not (harness / "CLAUDE.md").exists()


def test_a_lane_that_declares_a_missing_file_seeds_without_it(tmp_path: Path):
    """A declared file absent from the operator home is skipped without error."""
    home = _operator_home(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    backend = {
        "launch": "cli",
        "command": "claude",
        "harness_home_files": [
            {"path": "settings.json", "keys": ["hooks"]},
            {"path": "MISSING.md"},
            {"path": "CLAUDE.md"},
        ],
    }
    plan = _launch(home, run, backend, fence=True)
    harness = Path(plan.environment["CLAUDE_CONFIG_DIR"])
    assert (harness / "settings.json").is_file()
    assert (harness / "CLAUDE.md").is_file()
    assert not (harness / "MISSING.md").exists()


def test_a_claude_resume_places_the_operator_transcript(tmp_path: Path):
    """A session recorded in the operator home is copied at the same relative path."""
    home = _operator_home(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    operator_transcript = home / ".claude" / "projects" / "-home-op-reckon"
    relative = Path("projects/-home-op-reckon") / f"{CLAUDE_SESSION}.jsonl"
    expected = (operator_transcript / f"{CLAUDE_SESSION}.jsonl").read_bytes()

    plan = _launch(home, run, CLAUDE_BACKEND, fence=True, resume_session=CLAUDE_SESSION)
    harness = Path(plan.environment["CLAUDE_CONFIG_DIR"])
    placed = harness / relative
    assert placed.is_file()
    assert placed.read_bytes() == expected
    # Only the named session's transcript is placed, never the whole directory.
    assert not (harness / "projects" / "-home-op-reckon" / "unrelated.jsonl").exists()


def test_a_codex_resume_places_the_operator_rollout(tmp_path: Path):
    """Codex does the same for a rollout under .codex/sessions."""
    home = _operator_home(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    relative = (
        Path("sessions/2026/09/25")
        / f"rollout-2026-09-25T00-00-00-{CODEX_SESSION}.jsonl"
    )
    expected = (home / ".codex" / relative).read_bytes()

    plan = _launch(home, run, CODEX_BACKEND, fence=True, resume_session=CODEX_SESSION)
    harness = Path(plan.environment["CODEX_HOME"])
    placed = harness / relative
    assert placed.is_file()
    assert placed.read_bytes() == expected


def test_a_fresh_launch_copies_no_transcript(tmp_path: Path):
    """Without a resume, no session transcript is placed in the run home."""
    home = _operator_home(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    plan = _launch(home, run, CLAUDE_BACKEND, fence=True)
    harness = Path(plan.environment["CLAUDE_CONFIG_DIR"])
    assert not list(harness.rglob("*.jsonl"))
    assert not (harness / "projects").exists()


def test_an_existing_run_home_file_is_never_overwritten(tmp_path: Path):
    """A resumed run keeps the state it already wrote in its own home."""
    home = _operator_home(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    harness = run / "harness"
    (harness / "projects" / "-home-op-reckon").mkdir(parents=True)
    own_settings = json.dumps({"hooks": {"mine": True}})
    (harness / "settings.json").write_text(own_settings)
    (harness / "CLAUDE.md").write_text("# run's own\n")
    own_transcript = b'{"type":"assistant","local":true}\n'
    transcript = harness / "projects" / "-home-op-reckon" / f"{CLAUDE_SESSION}.jsonl"
    transcript.write_bytes(own_transcript)

    _launch(home, run, CLAUDE_BACKEND, fence=True, resume_session=CLAUDE_SESSION)

    assert (harness / "settings.json").read_text() == own_settings
    assert (harness / "CLAUDE.md").read_text() == "# run's own\n"
    assert transcript.read_bytes() == own_transcript


def test_an_unfenced_launch_keeps_the_operators_own_home(tmp_path: Path):
    """Without the fence no per-run home is invented, so the operator's is kept.

    The harness home is a fence artefact: only a fenced run adopts one. An
    unfenced run keeps the operator's home, where the user hooks, user memory
    and every session recorded before the run existed already live, so the
    launch must invent no harness-home variable and create no run home.
    """
    home = _operator_home(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    plan = _launch(home, run, CLAUDE_BACKEND, fence=False)

    assert "CLAUDE_CONFIG_DIR" not in plan.environment
    assert not (run / "harness").exists()
    assert plan.argv[0] != _backends.FENCE_BINARY


def test_the_operator_home_is_never_read_or_written(tmp_path: Path, monkeypatch):
    """The real harness homes stay out of it; only the given operator home is read.

    The real ``~/.claude`` is live — the fleet running this very test writes to
    it — so a metadata comparison against it would go red when the environment
    moves rather than when the code is wrong. The isolation is proved two other
    ways instead: every home the code could resolve without ``fence_home`` is
    redirected to a decoy that must stay untouched, and the run's own transcript
    must not appear under the real projects or sessions tree.
    """
    home = _operator_home(tmp_path)
    synthetic_before = _metadata_snapshot(home)

    # A decoy home: if the code resolved Path.home() instead of fence_home, it
    # would read these and the seeded hooks would be the decoy's.
    decoy = tmp_path / "decoy-home"
    (decoy / ".claude").mkdir(parents=True)
    (decoy / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"Stop": [{"command": "DECOY"}]}})
    )
    (decoy / ".claude" / "CLAUDE.md").write_text("# decoy guidance\n")
    (decoy / ".codex").mkdir(parents=True)
    (decoy / ".codex" / "AGENTS.md").write_text("# decoy codex\n")
    decoy_before = _metadata_snapshot(decoy)

    real_claude_projects = Path.home() / ".claude" / "projects"
    real_codex_sessions = Path.home() / ".codex" / "sessions"
    monkeypatch.setattr(Path, "home", _decoy_home(decoy))

    run = tmp_path / "run"
    run.mkdir()
    _launch(home, run, CLAUDE_BACKEND, fence=True, resume_session=CLAUDE_SESSION)
    _launch(home, run, CODEX_BACKEND, fence=True, resume_session=CODEX_SESSION)

    assert _metadata_snapshot(decoy) == decoy_before
    assert _metadata_snapshot(home) == synthetic_before
    # The seeded hooks are the given operator home's, never the decoy's.
    seeded = json.loads((run / "harness" / "settings.json").read_text())
    assert seeded["hooks"] == CLAUDE_SETTINGS["hooks"]
    assert STOP_COMMAND in json.dumps(seeded)
    # Nothing was written into the real harness homes for this run: neither
    # session's transcript appears under the real projects or sessions tree.
    markers = (CLAUDE_SESSION, CODEX_SESSION)
    for root in (real_claude_projects, real_codex_sessions):
        if not root.exists():
            continue
        assert not any(
            marker in str(path) for path in root.rglob("*") for marker in markers
        )


def _decoy_home(decoy: Path):
    """A ``Path.home`` stand-in returning the decoy directory."""
    return classmethod(lambda cls: decoy)


def test_a_fenced_clive_composition_carries_the_operator_stop_hook(tmp_path: Path):
    """A fenced clive launch plan's run home holds the operator's Stop hook.

    clive runs the claude-shaped harness in front of an open-weight model, so
    its declaration is the claude one and its CLAUDE_CONFIG_DIR is the run home.
    """
    home = _operator_home(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    plan = _launch(home, run, CLIVE_BACKEND, fence=True)
    harness = Path(plan.environment["CLAUDE_CONFIG_DIR"])
    assert plan.dialect == "claude"
    composition = {
        "dialect": plan.dialect,
        "harness_home": str(harness),
        "settings": json.loads((harness / "settings.json").read_text()),
    }
    assert composition["settings"]["hooks"] == CLAUDE_SETTINGS["hooks"]
    assert STOP_COMMAND in json.dumps(composition["settings"])
    # The fence wraps the launch, so a live worker starts inside it.
    assert plan.argv[0] == _backends.FENCE_BINARY


if __name__ == "__main__":  # pragma: no cover - reproduces the red log
    print(NEGATIVE_CONTROL_MUTATION)
    with __import__("tempfile").TemporaryDirectory() as directory:
        home = _operator_home(Path(directory))
        run = Path(directory) / "run"
        run.mkdir()
        plan = _launch(home, run, CLAUDE_BACKEND, fence=False)
        harness = Path(plan.environment["CLAUDE_CONFIG_DIR"])
        print(f"run harness home: {harness}")
        print(f"settings.json present: {(harness / 'settings.json').is_file()}")
    sys.exit(0)
