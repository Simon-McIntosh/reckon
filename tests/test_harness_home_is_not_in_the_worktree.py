"""A run's harness home is anchored to the run directory, never to the worktree.

A run's harness home is a folder inside the run directory — the manifest's
parent, and the one place a worker is always granted to write. A dispatch that
composes that home from anything but an absolute run directory anchors it to
the working directory of whichever process composed the plan instead: the
folder is created beside that process and the variable handed to the worker
names a relative path, which the harness then resolves against its own working
directory — the worktree the node was dispatched to. The worktree is left
carrying an untracked ``harness/`` or ``codex-home/`` at its root for the rest
of its life, and every merged worktree reads as dirty to the collector.

Each case below composes a real fenced plan from inside the worktree, the way a
worker stands in it, for both dialects, with the two manifest shapes a caller
that has no manifest path hands over — the empty string a resumed record yields
and a bare relative name — and asserts that no home is anchored to the worktree
at all; the last case then asserts that an absolute manifest path under the run
directory does anchor one there. Nothing in these cases resolves a path from
the process working directory, because that is the property under test.

Isolation: the real ``~/.config/reckon`` is live — the fleet running this very
test writes run pointers into it — so a metadata comparison against it would go
red when the environment moved rather than when the code was wrong. Every case
runs instead with ``RECKON_HOME`` removed (the one override that wins over the
given fence home), ``Path.home`` pointed at a decoy, and synthetic operator and
run directories, and the decoy and synthetic home are asserted untouched.

The declared negative control points the harness home back at the worktree in a
scratch copy; the dialect cases must fail with the home under the worktree.
Running this file against a copy whose ``reckon/_backends.py`` is the revision
before the anchoring change reproduces that red log.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from reckon import _backends

NEGATIVE_CONTROL_MUTATION = (
    "point the harness home back at the worktree in a scratch copy; the "
    "dialect cases must fail with the home under the worktree"
)

# dialect -> the variable its harness reads its config home from, and the name
# of the per-run folder inside the run directory that home is.
DIALECTS = {
    "claude": ("CLAUDE_CONFIG_DIR", "harness"),
    "codex": ("CODEX_HOME", "codex-home"),
}

# The two manifest shapes a caller with no manifest path hands over: the empty
# string a resumed record yields when it recorded none, and a bare relative
# name. Both name no location, so neither may anchor a home.
MANIFEST_SHAPES = {
    "no-manifest-path": "",
    "relative-manifest-path": "manifest.md",
}


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


def _operator_home(root: Path, dialect_name: str) -> Path:
    """Build the synthetic operator home the given dialect's harness reads.

    The files are real ones, so the metadata proof below is not vacuous: a
    write back into this home would move the snapshot, and a home with nothing
    to copy would let one pass unnoticed.
    """
    home = root / "operator-home"
    if home.is_dir():
        # Already built by an earlier case: rebuilding would rewrite the
        # metadata the isolation proof below compares, and the case would go
        # red on its own fixture rather than on the code.
        return home
    if dialect_name == "claude":
        claude = home / ".claude"
        claude.mkdir(parents=True)
        (claude / "settings.json").write_text('{"hooks": {"Stop": []}}\n')
        (claude / "CLAUDE.md").write_text("# operator guidance\n")
    else:
        codex = home / ".codex"
        codex.mkdir(parents=True)
        (codex / "AGENTS.md").write_text("# operator guidance\n")
    return home


@pytest.fixture(autouse=True)
def _decoy_config_home(tmp_path: Path, monkeypatch) -> Path:
    """Make every case reachable config home a decoy rather than the real one.

    ``RECKON_HOME`` is the one setting that wins over the fence home a case
    hands over, so an ambient value would send a lock directory into the
    operator's real config home; it is removed for every case. ``Path.home`` is
    redirected so a resolution that ignored the given home lands in the decoy
    and is visible rather than silent.
    """
    monkeypatch.delenv("RECKON_HOME", raising=False)
    decoy = tmp_path / "decoy-home"
    (decoy / ".config" / "reckon").mkdir(parents=True)
    (decoy / ".config" / "reckon" / "flight.yaml").write_text("# decoy\n")
    monkeypatch.setattr(Path, "home", _decoy_home(decoy))
    return decoy


def _compose(
    tmp_path: Path,
    monkeypatch,
    dialect_name: str,
    *,
    manifest_path: str | None,
):
    """Compose one fenced dispatch from inside its own worktree.

    ``manifest_path`` is handed through unchanged — ``None`` means an absolute
    manifest under the run directory, the shape a live run has.
    """
    root = tmp_path / dialect_name
    run = root / "runs" / "r-one"
    run.mkdir(parents=True, exist_ok=True)
    (run / "manifest.md").write_text("node: one\n")
    worktree = root / "worktrees" / "node"
    worktree.mkdir(parents=True, exist_ok=True)
    operator_home = _operator_home(root, dialect_name)
    manifest = str(run / "manifest.md") if manifest_path is None else manifest_path
    with monkeypatch.context() as patch:
        patch.chdir(worktree)
        plan = _backends.launch_plan(
            backend_name=dialect_name,
            backend={"launch": "cli", "command": dialect_name},
            prompt="do the node",
            worktree=str(worktree),
            manifest_path=manifest,
            fence=True,
            fence_home=operator_home,
        )
    return plan, run, worktree, operator_home


@pytest.mark.parametrize(
    "manifest_path",
    list(MANIFEST_SHAPES.values()),
    ids=list(MANIFEST_SHAPES),
)
def test_a_manifest_that_names_no_location_anchors_no_home(
    tmp_path: Path, monkeypatch, manifest_path: str
):
    """A manifest path that is not absolute puts no harness home anywhere."""
    for dialect_name, (variable, _folder) in DIALECTS.items():
        plan, _run, worktree, _home = _compose(
            tmp_path, monkeypatch, dialect_name, manifest_path=manifest_path
        )
        left = sorted(path.name for path in worktree.iterdir())
        assert left == [], (
            f"{dialect_name}: the worktree carries {left} after composing a "
            f"dispatch whose manifest path is {manifest_path!r}"
        )
        pinned = plan.environment.get(variable)
        assert pinned is None, (
            f"{dialect_name}: the dispatch pinned {variable}={pinned!r} for a "
            f"manifest path of {manifest_path!r}, which anchors no location"
        )


def test_an_absolute_manifest_anchors_the_home_under_the_run_directory(
    tmp_path: Path, monkeypatch
):
    """A live run's home resolves under the run directory, never the worktree."""
    for dialect_name, (variable, folder) in DIALECTS.items():
        plan, run, worktree, home = _compose(
            tmp_path, monkeypatch, dialect_name, manifest_path=None
        )
        assert plan.dialect == dialect_name
        harness = Path(plan.environment[variable])
        assert harness == run / folder
        assert harness.is_absolute()
        assert harness.is_dir()
        assert not harness.is_relative_to(worktree)
        assert list(worktree.iterdir()) == []
        # The anchor is a location transfer, not a content change: the run's
        # home carries what its harness reads, seeded from the operator's.
        assert _metadata_snapshot(harness)
        assert _metadata_snapshot(home)


def test_nothing_composes_a_path_into_the_operator_config_home(
    tmp_path: Path, monkeypatch
):
    """Every case composes against the given homes, never the real config home.

    A metadata snapshot of the real ``~/.config/reckon`` would go red whenever
    any session on this machine wrote a run into it, which is a statement about
    the environment rather than about this code. The proof is by construction
    and by decoy instead: ``RECKON_HOME`` is removed, any home the code could
    resolve without the given one resolves to the decoy, and the decoy and the
    synthetic operator home are both unchanged once every case has run.
    """
    assert "RECKON_HOME" not in os.environ
    # The decoy home the fixture installs as ``Path.home``.
    decoy = tmp_path / "decoy-home"
    decoy_before = _metadata_snapshot(decoy)
    operator_before: dict[Path, list[tuple]] = {}
    shapes = (None, *MANIFEST_SHAPES.values())

    for dialect_name in DIALECTS:
        for manifest_path in shapes:
            _plan, _run, _worktree, operator_home = _compose(
                tmp_path, monkeypatch, dialect_name, manifest_path=manifest_path
            )
            operator_before.setdefault(operator_home, _metadata_snapshot(operator_home))

    assert _metadata_snapshot(decoy) == decoy_before
    for operator_home, before in operator_before.items():
        assert _metadata_snapshot(operator_home) == before, (
            f"the synthetic operator home {operator_home} was written to"
        )
