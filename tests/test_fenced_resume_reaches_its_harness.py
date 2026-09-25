"""A resume of a fenced run reaches its harness, not the fence wrapped round it.

A fenced dispatch's argv is ``<fence> <binds...> -- <harness> ...`` and the
first element names the fence. A run record keeps the harness in two places:
the explicit ``command`` field, written from the composed argv's first element,
and the argv itself. For a fenced launch the field therefore holds the fence,
and a resume that fed it back as the harness composed a fence inside a fence —
bubblewrap was handed the harness's own flags, refused them, and the resumed run
produced no turn. The recorded ``command`` field stays the authority for an
unfenced or placed launch, where it is the only place the harness was recorded
beside the scheduler.

The fence is opt-in through ``dispatch.FENCE_WORKERS``, so a test of fenced
resume states the fence it tests: each case below sets ``FENCE_WORKERS`` true
before it resumes, rather than relying on a launcher default.

Four properties:

* a fenced clive resume yields a fenced argv whose inner command is the clive
  harness, carrying ``--resume`` and the recorded session id, with exactly one
  fence element and the fence never handed ``-p``;
* a fenced codex resume points ``CODEX_HOME`` at the run's own codex home and
  names ``codex exec ... resume``, so the session rollout is found;
* a record whose command field names a harness — an unfenced run, or a placed
  one whose field is the only place the harness survived the scheduler being
  wrapped onto the argv — still resolves to that harness;
* the declared negative control reads the recorded ``command`` field as the
  harness today, and the fenced clive case then yields the fence as the inner
  command and fails.

Running this file directly reproduces the red log: its first line is the
declared mutation, verbatim, and what follows is the observed composition.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

from reckon import _backends, crew

# ``reckon.crew.dispatch`` names the command function on the package, so the
# module is reached by import rather than by attribute.
dispatch_module = importlib.import_module("reckon.crew.dispatch")

# The declared mutation, printed verbatim as the red log's first line.
NEGATIVE_CONTROL_MUTATION = (
    "resume_plan reads the harness command from the run record command field "
    "as today; the fenced clive case must yield bwrap as the inner command "
    "and fail"
)

CONFIG = {
    "backends": {
        "clive": {
            "launch": "cli",
            "command": "clive",
            "sandbox": "worktree-full",
            "usable_input_window": 512_000,
        },
        "codex": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "usable_input_window": 512_000,
        },
    }
}


class Fixture:
    """One fenced dispatch, recorded the way dispatch writes it."""

    def __init__(self, root: Path, backend: str) -> None:
        self.root = root
        self.backend = backend
        self.run_id = f"r-fenced-{backend}"
        self.run_dir = root / "config" / "runs" / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.manifest = self.run_dir / "manifest.md"
        self.manifest.write_text(f"node: {self.run_id}\nstatus: in-progress\n")
        self.worktree = root / "trees" / self.run_id
        self.worktree.mkdir(parents=True, exist_ok=True)
        self.session_id = f"sess-fenced-{backend}"

    def dispatch_argv(self, *, fence: bool) -> list[str]:
        """The composition a fresh dispatch would spawn, fence included."""
        plan = _backends.launch_plan(
            backend_name=self.backend,
            backend=dict(CONFIG["backends"][self.backend]),
            prompt="do the node",
            worktree=str(self.worktree),
            manifest_path=str(self.manifest),
            fence=fence,
        )
        return list(plan.argv)

    def record(self, *, fence: bool, dialect: str | None = None) -> dict:
        argv = self.dispatch_argv(fence=fence)
        return {
            "run_id": self.run_id,
            "project": "",
            "repo": str(self.root / "ledger"),
            "worktree": str(self.worktree),
            "launch": "cli",
            "backend": self.backend,
            "dialect": dialect or self.backend,
            "sandbox": "worktree-full",
            "session_id": self.session_id,
            "manifest_path": str(self.manifest),
            "sandbox_write_roots": [],
            "argv": argv,
            # Written from the composed argv[0], exactly as dispatch writes it.
            "command": str(argv[0]) if argv else "",
        }

    def isolate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config_home = self.root / "config"
        config_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("RECKON_HOME", str(config_home))

    def resume(self, *, fence: bool, dialect: str | None = None):
        record = self.record(fence=fence, dialect=dialect)
        crew._write_json(crew.pointer_path(self.run_id), record)
        plan = crew.resume_plan(self.run_id, "continue", config=CONFIG)
        return record, plan


def _is_fence(token: object) -> bool:
    return Path(str(token)).name == _backends.FENCE_BINARY


def _fence_positions(argv: list[str]) -> list[int]:
    return [index for index, token in enumerate(argv) if _is_fence(token)]


def _separator(argv: list[str], fence_index: int) -> int:
    return argv.index("--", fence_index)


def _inner(argv: list[str], fence_index: int) -> list[str]:
    return argv[_separator(argv, fence_index) + 1 :]


def test_a_fenced_clive_resume_reaches_the_clive_harness(
    tmp_path: Path, monkeypatch
) -> None:
    """The resumed inner command is the harness, and the fence is composed once.

    On the defect the inner command is the fence binary itself: bubblewrap is
    handed the harness's own ``-p`` and refuses it, so the resumed run produces
    no turn. Naming the inner command is the whole difference between a resumed
    turn and a dead one.
    """
    fixture = Fixture(tmp_path, "clive")
    fixture.isolate(monkeypatch)
    monkeypatch.setattr(dispatch_module, "FENCE_WORKERS", True)
    record, plan = fixture.resume(fence=True)
    argv = list(plan.argv)

    positions = _fence_positions(argv)
    assert positions == [0], argv[:1]
    assert len(positions) == 1
    inner = _inner(argv, positions[0])
    assert Path(inner[0]).name == "clive"
    assert not _is_fence(inner[0])
    assert inner[inner.index("--resume") + 1] == fixture.session_id
    # The fence is never handed a harness flag of its own: nothing between the
    # fence element and its separator reads as one.
    assert "-p" not in argv[1 : _separator(argv, positions[0])]
    # The dispatch the record came from carried the same harness behind its
    # fence, so a resume composes the fence around the same command.
    dispatch_inner = _inner(record["argv"], 0)
    assert Path(dispatch_inner[0]).name == "clive"


def test_a_fenced_codex_resume_finds_the_runs_own_rollout(
    tmp_path: Path, monkeypatch
) -> None:
    """CODEX_HOME points at the run's own home and the inner command is codex.

    A codex session rollout lives under its home, so a resume that pointed the
    home anywhere else would reach no session. The home is the fence artefact
    seeded from the run's manifest directory, and the resumed argv must name the
    ``resume`` subcommand with the recorded session id.
    """
    fixture = Fixture(tmp_path, "codex")
    fixture.isolate(monkeypatch)
    monkeypatch.setattr(dispatch_module, "FENCE_WORKERS", True)
    _, plan = fixture.resume(fence=True)
    argv = list(plan.argv)

    home = plan.environment.get("CODEX_HOME")
    assert home, plan.environment
    run_home = _backends.harness_home("codex", fixture.run_dir)
    assert str(home) == str(run_home)
    assert str(fixture.run_dir) in str(home)

    positions = _fence_positions(argv)
    assert len(positions) == 1
    inner = _inner(argv, positions[0])
    assert Path(inner[0]).name == "codex"
    assert not _is_fence(inner[0])
    assert "exec" in inner
    assert inner[inner.index("resume") + 1] == fixture.session_id
    assert inner[-1] == "-"
    # The operator's login is bound read-only into the run's home, so the
    # resumed codex process still authenticates from inside the fence.
    binds = argv[: _separator(argv, positions[0])]
    assert "--ro-bind" in binds


def test_a_placed_records_command_field_is_still_the_harness(
    tmp_path: Path, monkeypatch
) -> None:
    """A record whose command field names a harness keeps its meaning.

    The refusal only fires on a command that names the fence. A placed launch
    records the harness in the field before the scheduler is wrapped onto the
    argv, so the field is the only place the harness survives — a reader taking
    the first word of that argv would translate the scheduler. This is the case
    the refusal must leave alone.
    """
    fixture = Fixture(tmp_path, "clive")
    fixture.isolate(monkeypatch)
    monkeypatch.setattr(dispatch_module, "FENCE_WORKERS", True)
    record = fixture.record(fence=False)
    record["argv"] = ["srun", "--job-name", fixture.run_id, *record["argv"]]
    assert Path(record["command"]).name == "clive"
    crew._write_json(crew.pointer_path(fixture.run_id), record)

    plan = crew.resume_plan(fixture.run_id, "continue", config=CONFIG)
    argv = list(plan.argv)

    positions = _fence_positions(argv)
    assert len(positions) == 1
    inner = _inner(argv, positions[0])
    assert Path(inner[0]).name == "clive"
    assert Path(inner[0]).name != "srun"
    assert inner[inner.index("--resume") + 1] == fixture.session_id


def test_the_negative_control_reads_the_command_field_as_today(
    tmp_path: Path, monkeypatch
) -> None:
    """The declared mutation: the recorded field is taken as the harness.

    With the fence off this is the whole rule, so removing the fence refusal
    restores it. The fenced clive case must then yield the fence as the inner
    command: if it did not, the refusal would not be the reason the inner
    command is the harness.
    """
    fixture = Fixture(tmp_path, "clive")
    fixture.isolate(monkeypatch)
    monkeypatch.setattr(dispatch_module, "FENCE_WORKERS", True)
    monkeypatch.setattr(dispatch_module, "_names_the_fence", lambda command: False)

    _, plan = fixture.resume(fence=True)
    argv = list(plan.argv)
    positions = _fence_positions(argv)
    inner = _inner(argv, positions[0])
    # The fence is composed as its own harness: two fence elements, the inner
    # one handed the harness's own flags. This is the dead resume.
    assert _is_fence(inner[0]), inner
    assert len(positions) == 2


def _negative_control_report(root: Path) -> list[str]:
    """Run the control without pytest's monkeypatch, restoring by hand."""
    fixture = Fixture(root, "clive")
    config_home = root / "config"
    config_home.mkdir(parents=True, exist_ok=True)
    saved_home = os.environ.get("RECKON_HOME")
    saved_probe = dispatch_module._names_the_fence
    saved_fence = dispatch_module.FENCE_WORKERS
    os.environ["RECKON_HOME"] = str(config_home)
    dispatch_module.FENCE_WORKERS = True
    dispatch_module._names_the_fence = lambda command: False
    try:
        record, plan = fixture.resume(fence=True)
    finally:
        dispatch_module._names_the_fence = saved_probe
        dispatch_module.FENCE_WORKERS = saved_fence
        if saved_home is None:
            os.environ.pop("RECKON_HOME", None)
        else:
            os.environ["RECKON_HOME"] = saved_home
    argv = [str(token) for token in plan.argv]
    positions = _fence_positions(argv)
    inner = _inner(argv, positions[0]) if positions else []
    return [
        f"record command field : {record['command']}",
        f"fence elements       : {len(positions)}",
        f"inner command        : {inner[0] if inner else ''}",
        f"inner is the fence   : {bool(positions) and _is_fence(inner[0])}",
        f"resume argv          : {json.dumps(argv)}",
    ]


if __name__ == "__main__":  # pragma: no cover - reproduces the red log
    print(NEGATIVE_CONTROL_MUTATION)
    with tempfile.TemporaryDirectory() as directory:
        for line in _negative_control_report(Path(directory)):
            print(line)
    sys.exit(0)
