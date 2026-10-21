"""A fenced codex review is handed only the roots the fence leaves writable.

A codex client prepares every root it is told writable — each ``--add-dir`` —
by creating the mount points it needs inside it, and refuses every command when
one of those roots is read-only. The fence seals the shared delivery stores
(the reports root and the review store) read-only and re-opens only a run's
*declared* paths beneath them, so a launch that hands a sealed store to the
dialect contradicts the fence.

One rule — :func:`reckon._worker_fence.fence_writable_roots` — states which
declared roots stay writable inside the fence. The fence's own grant loop uses
it, so its binds are unchanged, and the dialect is handed the same set, so an
``--add-dir`` the argv carries is always a path the fence binds writable or one
outside every sealed path.

The properties proved here, through the argv ``launch_plan`` composes:

* a fenced codex review whose declared write set holds the reports root, the
  review store, the run directory and a declared review-records path under the
  store carries no ``--add-dir`` for either sealed store, and still carries one
  for the review-records path beneath the store;
* every ``--add-dir`` it does carry is a path the fence binds writable or lies
  outside every sealed path;
* the fence's writable binds for that declared set are exactly the declared
  roots the stores do not seal, spelled out as literal expectations, so the
  factoring leaves the fence's binds alone;
* an unfenced launch with the same set keeps every ``--add-dir``.

The declared negative control restores the unfiltered writable set to the
dialect, and the assertion on the sealed stores' ``--add-dir`` is the one then
expected to fail.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import _backends, _worker_fence
from reckon.crew.review import review_store_root
from reckon.crew.runs import reports_dir

NEGATIVE_CONTROL_MUTATION = (
    "pass the unfiltered writable set to the dialect again; the new test fails "
    "on the reports root's --add-dir"
)


def _paths_after(argv: list[str], flag: str) -> list[str]:
    return [argv[index + 1] for index, token in enumerate(argv) if token == flag]


def _bind_pairs(argv: list[str], flag: str) -> list[list[str]]:
    return [
        argv[index + 1 : index + 3] for index, token in enumerate(argv) if token == flag
    ]


def _fence_segment(argv: list[str]) -> list[str]:
    """The fence's own argv: everything the wrapper emits before its ``--``."""
    return argv[: argv.index("--")]


def _granted_roots(fence: list[str]) -> list[str]:
    """The roots the fence re-binds writable, as ``--bind <root> <root>``."""
    return [
        source
        for source, destination in _bind_pairs(fence, "--bind")
        if source == destination
    ]


def _sealed_destinations(fence: list[str]) -> list[str]:
    """Every path the fence overlays read-only or hides outright."""
    sealed = [destination for _source, destination in _bind_pairs(fence, "--ro-bind")]
    return sealed + _paths_after(fence, "--tmpfs")


def _within_any(path: Path, roots: list[str]) -> bool:
    return any(path == Path(root) or path.is_relative_to(root) for root in roots)


class Fixture:
    """A stand-in operator home, its crew stores, a worktree and a live run."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.config = self.home / ".config" / "reckon"
        self.state = self.config / "state"
        self.run = self.config / "crew" / "runs" / "r1"
        self.checkout = self.home / "Code" / "proj"
        self.worktree = self.checkout / ".worktrees" / "wt"
        for directory in (self.state, self.run, self.checkout, self.worktree):
            directory.mkdir(parents=True, exist_ok=True)
        (self.checkout / ".git").mkdir(exist_ok=True)

        self.manifest = self.run / "manifest.md"
        self.manifest.write_text("")

    @property
    def records(self) -> Path:
        """The declared review-records path: one subject directory under the store.

        One subject directory under the review store, which the fence re-binds
        writable while the store root itself stays sealed. Read live rather than
        captured, because the store root follows ``RECKON_HOME`` and the fixture
        sets its own home when it isolates.
        """
        return review_store_root() / "plan" / "the-reviewed-plan"

    def isolate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name, value in (
            ("HOME", self.home),
            ("RECKON_HOME", self.config),
            ("RECKON_STATE_ROOT", self.state),
        ):
            monkeypatch.setenv(name, str(value))

    def declared_roots(self) -> list[Path]:
        return [self.run, reports_dir(), review_store_root(), self.records]

    def plan(self, *, fence: bool = True, **extra: object) -> _backends.LaunchPlan:
        backend = {
            "launch": "cli",
            "command": "codex",
            "dialect": "codex",
            # A review runs inside codex's own sandbox: the working directory is
            # the run directory the manifest lands in, and every other declared
            # root has to be named with --add-dir.
            "sandbox": _backends.READ_ONLY,
        }
        return _backends.launch_plan(
            backend_name="codex",
            backend=backend,
            prompt="p",
            worktree=str(self.worktree),
            manifest_path=str(self.manifest),
            writable_directories=[str(path) for path in self.declared_roots()],
            fence=fence,
            fence_home=str(self.home),
            **extra,
        )

    def lock(self) -> Path | None:
        """The plan-write lock namespace the launch seeds and grants."""
        return _worker_fence.seed_write_lock_namespace(str(self.home))

    def write_roots(self) -> list[str]:
        """The writable set ``launch_plan`` composes the fence with.

        The declared roots plus the plan-write lock namespace, which the launch
        seeds and grants so a fenced worker can write its own plan.
        """
        roots = [str(path) for path in self.declared_roots()]
        lock = self.lock()
        if lock is not None:
            roots.append(str(lock))
        return roots


def test_a_fenced_codex_review_is_handed_only_the_fences_writable_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sealed stores are withheld; a declared path beneath one is kept."""
    fixture = Fixture(tmp_path)
    fixture.isolate(monkeypatch)
    plan = fixture.plan()

    add_dirs = [Path(path) for path in _paths_after(plan.argv, "--add-dir")]
    reports = reports_dir().resolve()
    store = review_store_root().resolve()

    assert reports not in add_dirs, (
        f"the sealed reports root was named writable: {add_dirs}"
    )
    assert store not in add_dirs, (
        f"the sealed review store was named writable: {add_dirs}"
    )
    assert fixture.records.resolve() in add_dirs, (
        "a declared path beneath the review store must stay writable"
    )


def test_every_add_dir_is_bound_writable_or_outside_every_sealed_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the dialect is told writable, the fence really keeps writable."""
    fixture = Fixture(tmp_path)
    fixture.isolate(monkeypatch)
    plan = fixture.plan()

    fence = _fence_segment(plan.argv)
    granted = _granted_roots(fence)
    sealed = _sealed_destinations(fence)
    add_dirs = [Path(path) for path in _paths_after(plan.argv, "--add-dir")]
    assert add_dirs, "the launch named no writable root at all"

    for path in add_dirs:
        assert str(path) in granted or not _within_any(path, sealed), (
            f"{path} is neither bound writable by the fence nor outside every sealed path"
        )


def test_the_ambient_node_temp_root_is_withheld(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declared set names the node's own temp root, and the fence seals it.

    The delivery set adds the ambient temp root, so a launch can carry the
    node's own temporary directory among its declared roots. The fence seals
    that whole tree read-only, so handing it to the dialect contradicts the
    fence in the same way a sealed store does — while a declared root *beneath*
    it, such as the run's own scratch, stays writable and stays handed.
    """
    node_tmp = tmp_path / "node-tmp"
    node_tmp.mkdir()
    scratch = node_tmp / "reckon-crew-scratch" / "r1"
    scratch.mkdir(parents=True)
    monkeypatch.setattr(_worker_fence, "NODE_TMP_ROOT", node_tmp)

    fixture = Fixture(tmp_path)
    fixture.isolate(monkeypatch)
    declared = [str(path) for path in fixture.declared_roots()]
    plan = _backends.launch_plan(
        backend_name="codex",
        backend={
            "launch": "cli",
            "command": "codex",
            "dialect": "codex",
            "sandbox": _backends.READ_ONLY,
        },
        prompt="p",
        worktree=str(fixture.worktree),
        manifest_path=str(fixture.manifest),
        writable_directories=[*declared, str(node_tmp), str(scratch)],
        fence=True,
        fence_home=str(fixture.home),
    )

    fence = _fence_segment(plan.argv)
    granted = _granted_roots(fence)
    sealed = _sealed_destinations(fence)
    add_dirs = [Path(path) for path in _paths_after(plan.argv, "--add-dir")]

    assert node_tmp not in add_dirs, (
        f"the sealed node temp root was named writable: {add_dirs}"
    )
    assert scratch.resolve() in add_dirs, (
        "a declared root beneath the node temp root must stay handed"
    )
    for path in add_dirs:
        assert str(path) in granted or not _within_any(path, sealed), (
            f"{path} is neither bound writable by the fence nor outside every sealed path"
        )


def test_the_fences_declared_binds_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Factoring the rule out of the grant loop leaves the fence's binds alone.

    The expectation is spelled out rather than read from history: the fence must
    re-bind writable exactly the declared roots the sealed stores do not cover,
    plus the two roots the fence adds for itself — the worktree a worker commits
    in, and the runtime paths a codex launch writes beneath the sealed node temp
    root. The fence's own additions are named here as groups so a root that
    joins the declared set, or a bind that moves between the two, fails.
    """
    fixture = Fixture(tmp_path)
    fixture.isolate(monkeypatch)
    plan = fixture.plan()

    arguments = {
        "writable_directories": fixture.write_roots(),
        "worktree": str(fixture.worktree),
        "manifest_path": str(fixture.manifest),
        "home": str(fixture.home),
    }
    fence = _worker_fence.fence_argv(["true"], **arguments)
    assert _fence_segment(plan.argv) == _fence_segment(fence), (
        "the launch's fence is not the composition these inputs produce"
    )

    expected = {
        # The fixture's declared roots that neither sealed store covers: the run
        # directory the manifest lands in, the declared review-records path
        # beneath the store, and the plan-write lock namespace the launch seeds.
        fixture.run.resolve(),
        fixture.records.resolve(),
        # The fence's own additions: the worktree, and the runtime paths its
        # launch keeps writable beneath the sealed node temp root.
        fixture.worktree.resolve(),
        *[path.resolve() for path in _worker_fence.node_tmp_runtime_paths()],
    }
    lock = fixture.lock()
    if lock is not None:
        expected.add(lock.resolve())

    granted = {Path(root) for root in _granted_roots(fence)}
    assert granted == expected, "the fence's writable binds moved"

    # The sealed stores are never re-opened: a bind at or above one would expose
    # every other project's and run's reports and reviews through the seal.
    for store in (reports_dir(), review_store_root()):
        sealed = store.resolve()
        assert not any(
            path == sealed or sealed.is_relative_to(path) for path in granted
        ), f"the fence re-opened the sealed store {sealed}"


def test_an_unfenced_launch_keeps_every_add_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the fence there is no seal to contradict, so nothing is withheld."""
    fixture = Fixture(tmp_path)
    fixture.isolate(monkeypatch)
    plan = fixture.plan(fence=False)

    add_dirs = [Path(path) for path in _paths_after(plan.argv, "--add-dir")]
    assert reports_dir().resolve() in add_dirs
    assert review_store_root().resolve() in add_dirs
    assert fixture.records.resolve() in add_dirs
