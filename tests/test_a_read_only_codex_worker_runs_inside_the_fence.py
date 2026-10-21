"""A read-only codex worker is handed only roots the fence keeps writable.

A read-only codex launch runs inside a fence that seals the node's own ``/tmp``
read-only, and it also runs inside codex's own sandbox, which prepares every
root it is told writable by creating the mount points it needs inside it. When
one of those roots is read-only, codex refuses every command: on 2026-10-20 a
read-only review run failed each command with ``bwrap: Can't mkdir /tmp/.git:
Read-only file system`` and could not write its own manifest.

The contradiction had two halves. ``delivery_write_roots`` adds the ambient temp
root — ``/tmp`` where ``TMPDIR`` is unset — to the writable set, so the launch
carried ``--add-dir /tmp``. The fence seals ``/tmp`` read-only, and
``fence_writable_roots`` dropped only a root at or above a shared delivery
store, never one at or above the node-tmp seal. The fix drops a root at or above
the node-tmp seal too, so the dialect is handed only what the fence keeps
writable.

The properties proved here, through the argv ``launch_plan`` composes for a
read-only codex launch:

* no ``--add-dir`` names a path at or above a node-tmp seal — the ambient temp
  root is withheld;
* a root *beneath* the seal is still handed and bound writable, so the run's own
  scratch is not collaterally dropped;
* every ``--add-dir`` the argv carries is a path the fence binds writable or
  lies outside every sealed path.

The declared negative control restores the base ``fence_writable_roots`` — the
one that consults only the shared delivery stores — and the first assertion is
the one then expected to fail.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

import pytest

from reckon import _backends, _worker_fence

RUN_ID = "r-read-only-codex"

# The negative control: with the declared roots filtered against only the shared
# delivery stores, the ambient temp root is handed to codex again and the launch
# contradicts the fence.
NEGATIVE_CONTROL = os.environ.get("RECKON_TEST_NODE_TMP_ADDDIR_NEGATIVE") == "1"


def _add_dirs(argv: list[str]) -> list[str]:
    return [argv[index + 1] for index, flag in enumerate(argv) if flag == "--add-dir"]


def _bind_pairs(argv: list[str], flag: str) -> list[list[str]]:
    return [
        argv[index + 1 : index + 3] for index, word in enumerate(argv) if word == flag
    ]


def _granted_roots(fence: list[str]) -> set[str]:
    return {
        source
        for source, destination in _bind_pairs(fence, "--bind")
        if source == destination
    }


def _sealed_destinations(fence: list[str]) -> set[str]:
    return {destination for _source, destination in _bind_pairs(fence, "--ro-bind")}


_LIVE_SANDBOX_FLAGS = _backends._CodexDialect._sandbox_flags


def _base_sandbox_flags(dialect: object, tier: str | None) -> list[str]:
    """The rule before the change: no node-tmp exclusion on the read-only tier."""
    flags = list(_LIVE_SANDBOX_FLAGS(dialect, tier))  # type: ignore[arg-type]
    if tier == _backends.READ_ONLY:
        at = flags.index("-c")
        del flags[at : at + 2]
    return flags


def _base_fence_writable_roots(writable_directories: Iterable[str]) -> list[Path]:
    """The rule before the fix: only the shared delivery stores are consulted."""
    sealed = _worker_fence.shared_store_seal_roots()
    roots: list[Path] = []
    for directory in writable_directories:
        target = _worker_fence.resolved_destination(directory)
        if any(seal == target or seal.is_relative_to(target) for seal in sealed):
            continue
        roots.append(target)
    return roots


class Fixture:
    """A synthetic node tmp with a run scratch beneath it, and a live run."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.node_tmp = root / "node-tmp"
        self.scratch_root = self.node_tmp / "reckon-crew-scratch"
        self.scratch = self.scratch_root / RUN_ID
        self.scratch.mkdir(parents=True)
        self.fleet = self.node_tmp / f"{os.getuid()}-fleet"
        self.codex = self.node_tmp / ".codex"
        self.fleet.mkdir()
        self.codex.mkdir()
        self.home = root / "home"
        self.config = self.home / ".config" / "reckon"
        self.run = self.config / "crew" / "runs" / RUN_ID
        self.checkout = self.home / "Code" / "proj"
        self.worktree = self.checkout / ".worktrees" / "wt"
        for directory in (self.run, self.checkout, self.worktree):
            directory.mkdir(parents=True, exist_ok=True)
        (self.checkout / ".git").mkdir(exist_ok=True)
        self.manifest = self.run / "manifest.md"
        self.manifest.write_text("")

    def point_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_worker_fence, "NODE_TMP_ROOT", self.node_tmp)
        monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(self.scratch_root))
        monkeypatch.setenv("FLEET_RUNTIME_DIR", str(self.fleet))
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv("RECKON_HOME", str(self.config))
        monkeypatch.setenv("RECKON_STATE_ROOT", str(self.config / "state"))
        # The launching coordinator has TMPDIR unset, so delivery_write_roots
        # names the node's own temp root as the ambient writable root; point it
        # at the synthetic node tmp so the ambient root is the sealed one.
        monkeypatch.setattr(
            _backends.tempfile, "gettempdir", lambda: str(self.node_tmp)
        )

    def plan(self) -> _backends.LaunchPlan:
        from reckon.crew.dispatch_launch import worker_scratch_dir
        from reckon.crew.review import review_store_root
        from reckon.crew.runs import reports_dir

        roots = _backends.delivery_write_roots(
            run_directory=self.run,
            reports_directory=reports_dir(),
            manifest_path=self.manifest,
            review_store_directory=review_store_root(),
        )
        # A dispatch hands the run's own scratch directory (its ``TMPDIR``) as a
        # writable root, exactly as ``_fence_write_roots`` does; it sits beneath
        # the node-tmp seal and is the root the read-only tier must keep.
        declared = [str(path) for path in roots] + [str(worker_scratch_dir(RUN_ID))]
        backend = {
            "launch": "cli",
            "command": "codex",
            "dialect": "codex",
            "sandbox": _backends.READ_ONLY,
        }
        return _backends.launch_plan(
            backend_name="codex",
            backend=backend,
            prompt="p",
            worktree=str(self.worktree),
            manifest_path=str(self.manifest),
            writable_directories=declared,
            fence=True,
            fence_home=str(self.home),
        )


@pytest.fixture
def reverted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the declared negative control when the environment asks for it."""
    if NEGATIVE_CONTROL:
        monkeypatch.setattr(
            _backends, "fence_writable_roots", _base_fence_writable_roots
        )
        monkeypatch.setattr(
            _worker_fence, "fence_writable_roots", _base_fence_writable_roots
        )
        monkeypatch.setattr(
            _backends._CodexDialect, "_sandbox_flags", _base_sandbox_flags
        )


def _compose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Fixture, list[str]]:
    fixture = Fixture(tmp_path)
    fixture.point_the_environment(monkeypatch)
    return fixture, fixture.plan().argv


def test_no_add_dir_names_a_path_at_or_above_a_node_tmp_seal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reverted: None
) -> None:
    """The ambient temp root is withheld: the fence keeps it read-only."""
    _fixture, argv = _compose(tmp_path, monkeypatch)
    seal = _worker_fence.node_tmp_seal_root()
    assert seal is not None, "the synthetic node tmp must be sealable"

    offenders = [
        path
        for path in (Path(add) for add in _add_dirs(argv))
        if path == seal or seal.is_relative_to(path)
    ]
    assert not offenders, (
        f"the launch names {offenders} writable, but the fence seals {seal} read-only"
    )


def test_the_read_only_tier_excludes_the_node_temp_root_from_the_codex_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reverted: None
) -> None:
    """Codex's own policy would add the node temp root back as a writable root.

    ``workspace-write`` adds the node's own temporary directory to the writable
    set by default and protects a ``.git`` inside every root it holds, so it
    creates a mount point there whatever roots reckon hands it — and the fence
    seals that path read-only, which makes every command in the run fail. The
    dialect must therefore tell codex to exclude it.
    """
    _fixture, argv = _compose(tmp_path, monkeypatch)

    overrides = [
        argv[index + 1] for index, word in enumerate(argv) if word in {"-c", "--config"}
    ]
    assert "sandbox_workspace_write.exclude_slash_tmp=true" in overrides, (
        "the read-only tier must exclude the node temp root from codex's "
        f"writable set; the launch carries {overrides} as config overrides"
    )


def test_a_root_beneath_the_seal_is_still_handed_and_granted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run's own scratch sits beneath the seal and is not collateral."""
    fixture, argv = _compose(tmp_path, monkeypatch)
    fence = argv[: argv.index("--")]
    granted = _granted_roots(fence)
    add_dirs = {Path(add) for add in _add_dirs(argv)}

    assert fixture.scratch.resolve() in add_dirs, (
        "the run's own scratch directory must stay writable beneath the seal"
    )
    assert str(fixture.scratch.resolve()) in granted


def test_every_add_dir_is_bound_writable_or_outside_every_sealed_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reverted: None
) -> None:
    """What the dialect is told writable, the fence really keeps writable."""
    _fixture, argv = _compose(tmp_path, monkeypatch)
    fence = argv[: argv.index("--")]
    granted = _granted_roots(fence)
    sealed = _sealed_destinations(fence)
    add_dirs = [Path(add) for add in _add_dirs(argv)]
    assert add_dirs, "the launch named no writable root at all"

    for path in add_dirs:
        within_sealed = any(
            path == Path(root) or path.is_relative_to(root) for root in sealed
        )
        assert str(path) in granted or not within_sealed, (
            f"{path} is neither bound writable by the fence nor outside every "
            f"sealed path"
        )
