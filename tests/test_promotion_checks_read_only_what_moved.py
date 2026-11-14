"""A promotion's checks read only the paths its commits touch.

A promotion used to answer the figure-twin question by enumerating the whole
checkout: every path added since the run's base, then every path in the head
tree, to find an SVG beside a PNG. On a repository carrying thousands of files
that cost lands on every promotion, and it charges a run for paths a peer's
commit or a merged branch brought in.

These tests pin the two facts a promotion's own checks owe: the ledger loader
is called once, and the figure-twin check reads the commits the run cites. Each
test carries a positive control, so a clean reading is shown to be an absence
rather than a broken instrument.

Nothing here touches the operator's store: the crew home, the ledger and the
checkout are all built under the test's own temporary directory.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import ledger
from reckon.crew import promotion_checks, promotion_evidence
from reckon.crew.node import CrewError
from reckon.crew.runs import pointer_path
from tests.helpers.git import git as _git
from tests.test_a_promotion_does_not_read_the_whole_ledger_index import (
    PLAN,
    PROJECT,
    ROW_COUNT,  # noqa: F401 - the fixture's ledger size, named for a reader
    RUN_ID,
    _build_checkout,
    _seed,
)

# The checkout a promotion lands into carries this many tracked files, so a
# command that enumerates the tree costs visibly more than one that reads the
# paths a run touched.
TREE_FILES = 5000


class _GitRecorder:
    """Record every ``git`` argv run through ``subprocess.run``."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and argv and str(argv[0]).endswith("git"):
            self.calls.append([str(item) for item in argv])
        return self._original(argv, *args, **kwargs)

    def __enter__(self):
        self._original = subprocess.run
        self._patch = mock.patch.object(subprocess, "run", self.run)
        self._patch.start()
        return self

    def __exit__(self, *exc_info):
        self._patch.stop()
        return False


def _enumerates_the_tree(argv: list[str]) -> bool:
    """Whether a git argv asks git to enumerate a tree without naming paths.

    ``status`` walks the whole tree when nothing after ``--`` bounds it, and
    ``ls-tree``/``ls-files`` enumerate every path they are pointed at. A ``diff``
    with a single revision reads that commit's own change set, so it reads what
    moved; two revisions diff two trees, which is the base-to-head walk this
    measure exists to catch. `git -C <dir> …` puts the directory before the
    subcommand, so the tokens are matched against the subcommand vocabulary
    rather than taken positionally.
    """
    subcommands = {"status", "ls-tree", "ls-files", "diff", "diff-tree"}
    index = next(
        (position for position, item in enumerate(argv) if item in subcommands),
        None,
    )
    if index is None:
        return False
    subcommand = argv[index]
    if "--" in argv:
        tail = argv[argv.index("--") + 1 :]
        if [item for item in tail if not item.startswith("-")]:
            return False
    if subcommand in {"ls-tree", "ls-files"}:
        return True
    if subcommand == "status":
        return "--" not in argv
    revisions = [item for item in argv[index + 1 :] if not item.startswith("-")]
    return len(revisions) > 1


def _tree_walks(calls: list[list[str]]) -> list[list[str]]:
    return [argv for argv in calls if _enumerates_the_tree(argv)]


def _unbounded_working_tree_reads(calls: list[list[str]]) -> list[list[str]]:
    """Whether a git argv reads the working tree without bounding the paths.

    This is the cost the promotion pays per promotion: ``status``,
    ``ls-files`` and ``ls-tree`` enumerate what they are pointed at, and a
    ``diff`` with fewer than two revisions diffs the working tree against a
    commit. A two-commit diff reads no working tree at all — git compares the
    two tree objects and touches only the paths that differ — so it is not
    counted here even though :func:`_enumerates_the_tree` treats it as a walk.
    A ``--cached`` diff lists the index, which is one tree's worth of entries
    and no filesystem walk; the staging listing that issues it is measured
    separately, and that call site is outside this node's write scope.
    """
    subcommands = {"status", "ls-tree", "ls-files", "diff"}

    def unbounded(argv: list[str]) -> bool:
        index = next(
            (position for position, item in enumerate(argv) if item in subcommands),
            None,
        )
        if index is None:
            return False
        subcommand = argv[index]
        if "--" in argv:
            tail = argv[argv.index("--") + 1 :]
            if [item for item in tail if not item.startswith("-")]:
                return False
        if subcommand in {"ls-tree", "ls-files", "status"}:
            return True
        if "--cached" in argv:
            return False
        revisions = [
            item
            for item in argv[index + 1 :]
            if not item.startswith("-") and item != "--"
        ]
        return len(revisions) < 2

    return [argv for argv in calls if unbounded(argv)]


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.delenv("RECKON_RUN_ID", raising=False)
    return config_home


def _five_thousand_files(root: Path) -> None:
    """Track this many files so a whole-tree walk is a different shape."""
    bulk = root / "corpus"
    bulk.mkdir()
    for index in range(TREE_FILES):
        (bulk / f"shot-{index:05d}.txt").write_text(f"{index}\n", encoding="utf-8")
    _git(root, "add", "corpus")
    _git(root, "commit", "-q", "-m", "test: seed corpus")


def test_a_promotion_calls_the_ledger_loader_once(tmp_path: Path, home: Path) -> None:
    """One promotion over 2,000 ledger rows and a 5,000-file checkout."""
    root = _build_checkout(tmp_path / "repo")
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    _five_thousand_files(root)
    _seed(root, tmp_path)

    loader_calls: list[tuple] = []
    original_load_open = ledger.load_open

    def counting_load_open(*args, **kwargs):
        loader_calls.append((args, tuple(kwargs)))
        return original_load_open(*args, **kwargs)

    with mock.patch.object(ledger, "load_open", counting_load_open):
        result = CliRunner().invoke(
            cli_module.main,
            [
                "crew",
                "complete",
                "--wait",
                "--run",
                RUN_ID,
                "--gate",
                "not-run",
                "--outcome",
                "the fixture exercises the ledger reads a promotion makes",
                "--no-commit",
                "the sole deliverable is the report",
                "--checkout-path",
                str(root),
            ],
        )

    assert result.exit_code == 0, result.output
    assert loader_calls, "the instrument saw no ledger loader call at all"
    assert len(loader_calls) == 1, loader_calls


def _figure_commit(root: Path) -> tuple[str, str]:
    """Commit an SVG and its PNG twin, and return the base and the commit."""
    base = _git(root, "rev-parse", "HEAD")
    figures = root / "docs" / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    (figures / "diagram.svg").write_text("<svg/>\n", encoding="utf-8")
    (figures / "diagram.png").write_text("png\n", encoding="utf-8")
    _git(root, "add", "docs/figures")
    _git(root, "commit", "-q", "-m", "docs: add a figure")
    return base, _git(root, "rev-parse", "HEAD")


def _record(root: Path, tmp_path: Path, base: str, commit: str) -> dict:
    manifest = tmp_path / "manifests" / "figure.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: figure-node\n"
        "status: complete\n"
        "changed_paths: docs/figures/\n"
        f"commits: {commit}\n",
        encoding="utf-8",
    )
    return {
        "run_id": "r-20261114T000000000000-figure-a",
        "project": PROJECT,
        "repo": str(root),
        "worktree": str(root),
        "base_sha": base,
        "manifest_path": str(manifest),
        "node": {"id": "figure-node", "plan": PLAN, "section": "s3"},
    }


def _declare_write_paths(paths: list[str]) -> None:
    """Give the fixture's run a declaration, so the reads have a root to bound;
    a run that declares nothing has nothing its changes can be charged to."""
    pointer_file = pointer_path(RUN_ID)
    pointer = json.loads(pointer_file.read_text(encoding="utf-8"))
    pointer["node"]["write_paths"] = list(paths)
    pointer_file.write_text(json.dumps(pointer), encoding="utf-8")


def _promote(root: Path, home: Path) -> tuple[object, _GitRecorder]:
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    recorder = _GitRecorder()
    with recorder:
        result = CliRunner().invoke(
            cli_module.main,
            [
                "crew",
                "complete",
                "--wait",
                "--run",
                RUN_ID,
                "--gate",
                "not-run",
                "--outcome",
                "the fixture exercises the working-tree reads a promotion makes",
                "--no-commit",
                "the sole deliverable is the report",
                "--checkout-path",
                str(root),
            ],
        )
    return result, recorder


def test_a_full_promotion_reads_no_unbounded_working_tree(
    tmp_path: Path, home: Path
) -> None:
    """A promotion of the 2,000-row / 5,000-file fixture bounds every status."""
    root = _build_checkout(tmp_path / "repo")
    _five_thousand_files(root)
    _seed(root, tmp_path)
    # A declared root that the fixture never writes: the read is bounded, and
    # nothing in it is the run's uncommitted work.
    _declare_write_paths(["docs/report"])

    result, recorder = _promote(root, home)

    assert result.exit_code == 0, result.output
    assert recorder.calls, "the instrument saw no git call at all"
    unbounded = _unbounded_working_tree_reads(recorder.calls)
    # The residue save is the one whole-tree reader a promotion is right to
    # make: it preserves whatever the tree holds before the removal, so a
    # bounded read there would drop work that no run declared. It is counted
    # rather than excused — exactly one, and nothing else unbounded.
    residue = [argv for argv in unbounded if argv[1:3] == ["status", "--porcelain=v1"]]
    assert [argv for argv in unbounded if argv not in residue] == [], unbounded
    assert len(residue) == 1, residue
    status_calls = [
        argv
        for argv in recorder.calls
        if "status" in argv and argv not in residue
    ]
    assert status_calls, "no status call at all, so nothing was measured"
    assert all("--" in argv for argv in status_calls), status_calls


def test_dirt_outside_the_declared_paths_is_not_charged_to_the_run(
    tmp_path: Path, home: Path
) -> None:
    """Bounding the read must not make the refusal stop firing at a root.

    The committed-work guard charges a run only for the paths it declared, so a
    peer's stray edit beside them reaches the boundary check instead of this
    refusal. The same instrument is shown catching dirt at a declared path, so
    the clean reading is an absence rather than a broken reader.
    """
    root = _build_checkout(tmp_path / "repo")
    _five_thousand_files(root)
    base = _git(root, "rev-parse", "HEAD")
    (root / "corpus" / "shot-00001.txt").write_text("stray edit\n", encoding="utf-8")
    record = {
        "run_id": "r-20261114T000000000000-stray-a",
        "project": PROJECT,
        "repo": str(root),
        "worktree": str(root),
        "base_sha": base,
        "node": {"id": "stray-node", "write_paths": ["docs/"]},
    }
    recorder = _GitRecorder()
    with recorder:
        promotion_evidence._require_commits_beyond_base(
            record["run_id"], record, ()
        )

    statuses = [argv for argv in recorder.calls if "status" in argv]
    assert statuses, "the instrument saw no status call at all"
    assert all("--" in argv for argv in statuses), statuses

    (root / "docs" / "deliverable.txt").write_text("declared\n", encoding="utf-8")
    with pytest.raises(CrewError) as refused:
        promotion_evidence._require_commits_beyond_base(record["run_id"], record, ())
    assert "docs/deliverable.txt" in str(refused.value), refused.value


def test_the_figure_twin_check_reads_only_the_run_commits(
    tmp_path: Path, home: Path
) -> None:
    """The twin is found from the run's own commit, and no tree is walked."""
    root = _build_checkout(tmp_path / "repo")
    _five_thousand_files(root)
    base, commit = _figure_commit(root)
    record = _record(root, tmp_path, base, commit)

    recorder = _GitRecorder()
    with recorder, pytest.raises(CrewError) as refused:
        promotion_checks._require_no_png_twin_of_an_svg(record["run_id"], record)

    assert "diagram.png" in str(refused.value), refused.value
    assert recorder.calls, "the instrument saw no git call at all"
    assert _tree_walks(recorder.calls) == [], _tree_walks(recorder.calls)
