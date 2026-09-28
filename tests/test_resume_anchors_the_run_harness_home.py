"""A resume anchors the run's harness home where the dispatch anchored it.

A launch derives the run's directory from its manifest path: the harness home
is the manifest's parent plus the dialect's own folder, and the fence roots are
built from the same parent. A resume rebuilds its launch from the live pointer,
and that pointer does not always carry a usable path — the top-level field may
be absent or null, with only the node definition remembering what the dispatch
composed. A resume that hands over an empty string therefore anchors the home
to the working directory of the launch-composing process; because the worker's
harness resolves the variable it is given, the home then lands wherever that
process stood, and a *fenced* resume of a codex run falls back to the
operator's sealed home and exits "Read-only file system".

Two dialects, because the harnesses disagree on all three names: clive speaks
the claude dialect and reads ``CLAUDE_CONFIG_DIR`` for a home it calls
``harness``; codex reads ``CODEX_HOME`` for ``codex-home``.

Every case composes the dispatch the run would have had, from the same
absolute manifest, and requires the resumed launch to name the same home
string it named — the comparison is between two compositions rather than
against a literal, so a change of folder or variable on either side cannot
leave this file asserting a stale pair. The isolation is by construction:
``RECKON_HOME`` points at a temporary home, so every pointer, run directory
and lock lands there instead of in the real ``~/.config/reckon``, and
``Path.home`` is a decoy, so the operator home the harness homes are seeded
from is synthetic. The decoy is asserted untouched afterwards.

The declared negative control reads the pointer's own top-level field alone,
as the resume did before the resolution: the null-path cases must then compose
no harness home at all and fail here.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from reckon import _backends, crew

# ``reckon.crew.dispatch`` names the command function on the package, so the
# module is reached by import rather than by attribute.
dispatch_module = importlib.import_module("reckon.crew.dispatch")

NEGATIVE_CONTROL_MUTATION = (
    "read the pointer's own top-level manifest path alone; the null-path "
    "cases must compose no harness home and fail here"
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

# backend -> the dialect it resolves to, the variable that dialect reads its
# config home from, and the folder it keeps that home under.
HARNESS_HOME = {
    "clive": ("claude", "CLAUDE_CONFIG_DIR", "harness"),
    "codex": ("codex", "CODEX_HOME", "codex-home"),
}


class Fixture:
    """One dispatched run, recorded the way dispatch writes it."""

    def __init__(self, root: Path, backend: str, monkeypatch: pytest.MonkeyPatch):
        self.root = root
        config_home = root / "config"
        config_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("RECKON_HOME", str(config_home))
        # The fence is stated rather than inherited from the launcher default:
        # a fenced resume is the case whose codex home falls back to the
        # operator's sealed one when the composed home is missing.
        monkeypatch.setattr(dispatch_module, "FENCE_WORKERS", True)
        self.config_home = config_home
        self.operator_home = root / "operator-home"
        self._seed_operator_home()
        monkeypatch.setattr(Path, "home", _decoy_home(self.operator_home))
        self.backend = backend
        self.dialect, self.variable, self.folder = HARNESS_HOME[backend]
        self.run_id = f"r-resume-{backend}"
        self.run_dir = crew.run_dir(self.run_id)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.manifest = self.run_dir / "manifest.md"
        self.manifest.write_text(f"node: {self.run_id}\nstatus: in-progress\n")
        # The run's own worker record, written by the supervisor that spawned
        # it: this is what identifies the run directory the fallback anchors to.
        crew._write_json(
            self.run_dir / "worker.json",
            {
                "run_id": self.run_id,
                "attempt": 1,
                "pid": 1,
                "pid_start_time": None,
                "launched_at": "2026-09-28T10:00:00Z",
                "backend": backend,
                "argv": [],
            },
        )
        self.worktree = root / "trees" / self.run_id
        self.worktree.mkdir(parents=True, exist_ok=True)
        self.session_id = f"sess-resume-{backend}"

    def _seed_operator_home(self) -> None:
        """The operator dot directories a harness home is seeded from."""
        claude = self.operator_home / ".claude"
        claude.mkdir(parents=True, exist_ok=True)
        (claude / "settings.json").write_text('{"hooks": {"Stop": []}}\n')
        codex = self.operator_home / ".codex"
        codex.mkdir(parents=True, exist_ok=True)
        (codex / "AGENTS.md").write_text("# operator guidance\n")

    def dispatch_plan(self):
        """The plan a fresh dispatch composes, fence included."""
        return _backends.launch_plan(
            backend_name=self.backend,
            backend=dict(CONFIG["backends"][self.backend]),
            prompt="do the node",
            worktree=str(self.worktree),
            manifest_path=str(self.manifest),
            fence=True,
        )

    def record(self) -> dict:
        """The live pointer a dispatch of this run would leave behind."""
        argv = list(self.dispatch_plan().argv)
        return {
            "run_id": self.run_id,
            "project": "",
            "repo": str(self.root / "ledger"),
            "worktree": str(self.worktree),
            "launch": "cli",
            "backend": self.backend,
            "dialect": self.dialect,
            "sandbox": "worktree-full",
            "session_id": self.session_id,
            "sandbox_write_roots": [],
            "argv": argv,
            "command": str(argv[0]) if argv else "",
        }

    def resume(self, record: dict):
        crew._write_json(crew.pointer_path(self.run_id), record)
        return crew.resume_plan(self.run_id, "continue", config=CONFIG)


def _decoy_home(decoy: Path):
    """A ``Path.home`` stand-in returning the decoy directory."""
    return classmethod(lambda cls: decoy)


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


@pytest.mark.parametrize("backend", list(HARNESS_HOME))
def test_a_resume_reaches_the_home_the_dispatch_composed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    """A null top-level path still resolves to the run's own manifest.

    The record keeps the dispatch's manifest in its node definition — the way
    a pointer whose top-level field was written null does — and the resumed
    launch must name the same home the dispatch named: the run's harness
    folder, absolute, inside the run directory and outside the worktree.
    """
    fixture = Fixture(tmp_path, backend, monkeypatch)
    dispatch_home = fixture.dispatch_plan().environment[fixture.variable]
    record = fixture.record()
    record["manifest_path"] = None
    record["node"] = {"manifest_path": str(fixture.manifest)}

    plan = fixture.resume(record)

    home = plan.environment.get(fixture.variable)
    assert home == dispatch_home, plan.environment
    assert Path(home) == fixture.run_dir / fixture.folder
    assert Path(home).is_absolute()
    assert Path(home).is_dir()
    assert Path(home).is_relative_to(fixture.run_dir)
    assert not Path(home).is_relative_to(fixture.worktree)
    # The anchor is a location, not an effect on the tree the node was
    # dispatched to: the worktree gains nothing from the resume.
    assert [path.name for path in fixture.worktree.iterdir()] == []
    assert plan.resumed_session == fixture.session_id


@pytest.mark.parametrize("backend", list(HARNESS_HOME))
def test_a_pointer_that_records_no_manifest_path_falls_back_to_the_run_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    """A record that carries no path anywhere anchors to its own run directory.

    The run directory is absolute by construction and is the one holding the
    run's worker record, so a resume of a record with no manifest path at all
    still has a home to reach — and it is not the directory of whichever
    process composed the launch.
    """
    fixture = Fixture(tmp_path, backend, monkeypatch)
    record = fixture.record()
    record["manifest_path"] = None

    plan = fixture.resume(record)

    home = plan.environment.get(fixture.variable)
    assert home is not None, plan.environment
    assert Path(home) == fixture.run_dir / fixture.folder
    assert Path(home).is_absolute()
    assert not Path(home).is_relative_to(fixture.worktree)
    assert [path.name for path in fixture.worktree.iterdir()] == []


@pytest.mark.parametrize("backend", list(HARNESS_HOME))
def test_a_resume_writes_nothing_outside_the_temporary_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    """The evidence is composed against synthetic homes, not the real ones.

    ``RECKON_HOME`` is the one setting that wins over every derived config
    home, so with it pointed at the temporary root no pointer, run directory
    or lock can reach the real ``~/.config/reckon``; the operator home the
    harness homes are seeded from is a decoy rather than the operator's own
    dot directories. Only the decoy may move, and only inside the run's home.
    """
    fixture = Fixture(tmp_path, backend, monkeypatch)
    assert os.environ["RECKON_HOME"] == str(fixture.config_home)
    assert str(crew.pointer_path(fixture.run_id)).startswith(str(fixture.config_home))
    assert str(crew.run_dir(fixture.run_id)).startswith(str(fixture.config_home))
    operator_before = _metadata_snapshot(fixture.operator_home)
    record = fixture.record()
    record["manifest_path"] = None
    record["node"] = {"manifest_path": str(fixture.manifest)}

    fixture.resume(record)

    # The decoy operator home is read from and never written to: a home seeded
    # back into it would move this snapshot.
    assert _metadata_snapshot(fixture.operator_home) == operator_before
