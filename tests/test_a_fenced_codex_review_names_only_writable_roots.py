"""A fenced codex review is handed only the roots the fence leaves writable.

codex 0.163.0 prepares every root it is told writable — ``--add-dir`` — by
creating the mount points it needs inside it, and refuses *every* command when
one of those roots cannot be written. The fence seals the shared delivery
stores (the reports root and the review store) read-only and re-opens only a
run's *declared* paths beneath them, so a launch that hands a sealed store to
the dialect contradicts the fence: on 2026-10-18 five power_convertor reviews
failed on ``bwrap: Can't mkdir .../crew/reports/.aws: Read-only file system``.

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
* the fence's writable binds equal the ones the pre-change rule composes for
  the same inputs, read from the revision that introduced the rule;
* an unfenced launch with the same set keeps every ``--add-dir``.

The declared negative control restores the unfiltered writable set to the
dialect; the reports root's ``--add-dir`` assertion must then fail, and the red
log's first line is the mutation string below, verbatim.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from reckon import _backends, _worker_fence
from reckon.crew.review import review_store_root
from reckon.crew.runs import reports_dir

NEGATIVE_CONTROL_MUTATION = (
    "pass the unfiltered writable set to the dialect again; the new test fails "
    "on the reports root's --add-dir"
)

FENCE_RULE_SYMBOL = "def fence_writable_roots"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _paths_after(argv: list[str], flag: str) -> list[str]:
    return [argv[index + 1] for index, token in enumerate(argv) if token == flag]


def _bind_pairs(argv: list[str], flag: str) -> list[list[str]]:
    return [argv[index + 1 : index + 3] for index, token in enumerate(argv) if token == flag]


def _fence_segment(argv: list[str]) -> list[str]:
    """The fence's own argv: everything the wrapper emits before its ``--``."""
    return argv[: argv.index("--")]


def _granted_roots(fence: list[str]) -> list[str]:
    """The roots the fence re-binds writable, as ``--bind <root> <root>``."""
    return [source for source, destination in _bind_pairs(fence, "--bind") if source == destination]


def _sealed_destinations(fence: list[str]) -> list[str]:
    """Every path the fence overlays read-only or hides outright."""
    sealed = [destination for _source, destination in _bind_pairs(fence, "--ro-bind")]
    return sealed + _paths_after(fence, "--tmpfs")


def _within_any(path: Path, roots: list[str]) -> bool:
    return any(path == Path(root) or path.is_relative_to(root) for root in roots)


@pytest.fixture(scope="session")
def base_fence_module(tmp_path_factory: pytest.TempPathFactory) -> ModuleType:
    """The fence as it stood before the rule, loaded from the introducing revision.

    ``git log -S`` names the commit that introduced the rule; the revision
    before it composes the binds this change must leave untouched. Read from
    history rather than restated, so the base arm moves with the repository
    instead of being a literal that rots.
    """
    repository = Path(__file__).resolve().parents[1]
    introducing = _git(
        repository, "log", "-1", "--format=%H", "-S", FENCE_RULE_SYMBOL, "--",
        "reckon/_worker_fence.py",
    )
    revision = f"{introducing}^" if introducing else "HEAD"
    try:
        blob = subprocess.run(
            ["git", "-C", str(repository), "show", f"{revision}:reckon/_worker_fence.py"],
            check=True,
            capture_output=True,
        ).stdout
    except subprocess.CalledProcessError as exc:  # pragma: no cover - shallow clone
        pytest.skip(f"the base revision {revision} is not readable: {exc}")
    source = blob.decode()
    if FENCE_RULE_SYMBOL in source:  # pragma: no cover - rule already in the base
        pytest.skip(f"base revision {revision} already carries the rule; nothing to compare")
    staged = tmp_path_factory.mktemp("base-fence") / "_worker_fence_base.py"
    staged.write_bytes(blob)
    spec = importlib.util.spec_from_file_location("_worker_fence_base", staged)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_worker_fence_base"] = module
    spec.loader.exec_module(module)
    return module


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

        # The declared review-records path: one subject directory under the
        # review store, which the fence re-binds writable while the store root
        # itself stays sealed.
        self.records = review_store_root() / "plan" / "the-reviewed-plan"

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

    def write_roots(self) -> list[str]:
        """The writable set ``launch_plan`` composes the fence with.

        The declared roots plus the plan-write lock namespace, which the launch
        seeds and grants so a fenced worker can write its own plan.
        """
        roots = [str(path) for path in self.declared_roots()]
        lock = _worker_fence.seed_write_lock_namespace(str(self.home))
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

    assert reports not in add_dirs, f"the sealed reports root was named writable: {add_dirs}"
    assert store not in add_dirs, f"the sealed review store was named writable: {add_dirs}"
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


def test_the_fences_binds_are_unchanged_from_the_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    base_fence_module: ModuleType,
) -> None:
    """Factoring the rule out of the grant loop leaves the fence's binds alone."""
    fixture = Fixture(tmp_path)
    fixture.isolate(monkeypatch)
    plan = fixture.plan()

    arguments = dict(
        writable_directories=fixture.write_roots(),
        worktree=str(fixture.worktree),
        manifest_path=str(fixture.manifest),
        home=str(fixture.home),
    )
    head = _worker_fence.fence_argv(["true"], **arguments)
    base = base_fence_module.fence_argv(["true"], **arguments)

    assert _fence_segment(plan.argv) == _fence_segment(head), (
        "the launch's fence is not the composition these inputs produce"
    )
    assert _granted_roots(head) == _granted_roots(base), "the fence's writable binds moved"


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