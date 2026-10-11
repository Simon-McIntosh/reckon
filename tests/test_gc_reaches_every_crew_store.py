"""gc sweeps report trees, run locks and run directories with no ledger row.

Three stores the crew home keeps outside the ledger grow with every run and
nothing bounds them: a review's report tree, a run's advisory lock file and a
run directory never written to the ledger. gc reports each past its retention
and removes it only under ``--apply``, and no removal touches a store a live
run still holds. Every fixture is synthesised under an isolated ``RECKON_HOME``,
so a case that inspected the real crew home would have nothing of its own to
look at.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from click.testing import CliRunner

from reckon import cli, ledger
from reckon.crew import plan_review
from reckon.crew.routing import _workspace_roots
from reckon.crew.runs import crew_home, reports_dir

PROJECT = "gc-store"
SLUG = "fixture"
RETENTION = 30
OLD_DAYS = 60


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def repository(root: Path) -> Path:
    """A minimal repository with a committed tree, so gc's HEAD resolves."""
    repo = root / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "seed.txt").write_text("seed\n")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "test: seed")
    (repo / "docs" / "state" / PROJECT).mkdir(parents=True)
    return repo


def _age(path: Path, days: float) -> None:
    """Backdate a file or a whole tree by the given number of days."""
    stamp = (datetime.now(tz=UTC) - timedelta(days=days)).timestamp()
    if path.is_dir():
        for child in sorted(path.rglob("*"), reverse=True):
            os.utime(child, (stamp, stamp))
    os.utime(path, (stamp, stamp))


def write_report_tree(repo: Path, run_id: str, *, age_days: float, committed: bool):
    """A plan-review report tree, optionally with its committed record filed."""
    directory = plan_review.review_report_directory(PROJECT, SLUG, run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / plan_review._REVIEW_REPORT_NAME).write_text("RUBRIC wiring: pass\n")
    (directory / plan_review._REVIEW_SIDECAR_NAME).write_text(
        json.dumps({"run_id": run_id}), encoding="utf-8"
    )
    (directory / plan_review._REVIEW_SNAPSHOT_NAME).write_text(
        "<html></html>", encoding="utf-8"
    )
    if committed:
        record = (
            repo
            / "docs"
            / "state"
            / PROJECT
            / "reviews"
            / "plan"
            / SLUG
            / f"{run_id}.json"
        )
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({"review_run_id": run_id}), encoding="utf-8")
    _age(directory, age_days)
    return directory


def write_general_tree(run_id: str, *, age_days: float) -> Path:
    """A run-keyed report tree outside the plan-review family."""
    tree = reports_dir() / PROJECT / run_id
    tree.mkdir(parents=True, exist_ok=True)
    (tree / "report.md").write_text("general report\n", encoding="utf-8")
    _age(tree, age_days)
    return tree


def write_pointer(run_id: str, *, worktree: Path | None = None) -> None:
    live = crew_home() / "live"
    live.mkdir(parents=True, exist_ok=True)
    record: dict = {"run_id": run_id, "phase": "working"}
    if worktree is not None:
        record["worktree"] = str(worktree)
    (live / f"{run_id}.json").write_text(json.dumps(record), encoding="utf-8")


def _worktree(repo: Path, name: str) -> Path:
    """A managed tree registered at the repository's head, clean and integrated."""
    root = _workspace_roots(repo)[0]
    tree = root / name
    tree.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", "--detach", str(tree), "HEAD")
    return tree


def record_run(repo: Path, run_id: str) -> None:
    ledger.append_run(
        PROJECT,
        ledger.build_record(run_id=run_id, plan="plan-a", gate="passed", node="node-a"),
        root=repo,
    )


def write_lock(run_id: str, *, age_days: float) -> Path:
    lock = crew_home() / "locks" / f"{run_id}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("", encoding="utf-8")
    _age(lock, age_days)
    return lock


def write_run_dir(run_id: str, *, age_days: float) -> Path:
    directory = crew_home() / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "stream.jsonl").write_text("{}\n", encoding="utf-8")
    _age(directory, age_days)
    return directory


def gc(repo: Path, *, apply: bool, run: str | None = None) -> dict:
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "gc",
            "--repo",
            str(repo),
            "--project",
            PROJECT,
            "--retention-days",
            str(RETENTION),
            *(["--run", run] if run else []),
            *(["--apply"] if apply else []),
        ],
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def planted_stores(repo: Path):
    """Plant one past-retention, one fresh and one live-run entry per row set."""
    report_old = write_report_tree(
        repo, "pr-report-old", age_days=OLD_DAYS, committed=True
    )
    report_fresh = write_report_tree(
        repo, "pr-report-fresh", age_days=3, committed=True
    )
    report_live = write_general_tree("gen-report-live", age_days=OLD_DAYS)
    write_pointer("gen-report-live")

    for run_id in ("lock-old", "lock-fresh", "lock-live"):
        record_run(repo, run_id)
    lock_old = write_lock("lock-old", age_days=OLD_DAYS)
    lock_fresh = write_lock("lock-fresh", age_days=1)
    lock_live = write_lock("lock-live", age_days=OLD_DAYS)
    write_pointer("lock-live")

    dir_old = write_run_dir("ud-old", age_days=OLD_DAYS)
    dir_fresh = write_run_dir("ud-fresh", age_days=1)
    dir_live = write_run_dir("ud-live", age_days=OLD_DAYS)
    write_pointer("ud-live")

    return {
        "report_old": report_old,
        "report_fresh": report_fresh,
        "report_live": report_live,
        "lock_old": lock_old,
        "lock_fresh": lock_fresh,
        "lock_live": lock_live,
        "dir_old": dir_old,
        "dir_fresh": dir_fresh,
        "dir_live": dir_live,
    }


def _rows(report: dict) -> list[dict]:
    return [
        *report["report_trees"],
        *report["lock_files"],
        *report["unledgered_run_directories"],
    ]


def test_gc_reports_and_removes_each_unbounded_store(tmp_path, monkeypatch):
    """Each row set lists exactly its one past-retention entry without a live
    run, and ``--apply`` removes exactly those, printing each path and size."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    repo = repository(tmp_path)
    stores = planted_stores(repo)
    report_old = stores["report_old"]
    raw_old = report_old / plan_review._REVIEW_REPORT_NAME

    dry = gc(repo, apply=False)
    assert dry["dry_run"] is True

    # The reports row set lists the past plan-review tree, not the fresh one and
    # not the general tree whose run still holds a live pointer.
    report_paths = {row["path"] for row in dry["report_trees"]}
    assert report_paths == {str(raw_old)}
    assert str(stores["report_fresh"] / plan_review._REVIEW_REPORT_NAME) not in (
        report_paths
    )
    assert str(stores["report_live"]) not in report_paths

    # One past-retention lock, and only that one.
    assert {row["path"] for row in dry["lock_files"]} == {str(stores["lock_old"])}
    # One unledgered run directory, and only that one.
    assert {row["path"] for row in dry["unledgered_run_directories"]} == {
        str(stores["dir_old"])
    }

    # Exactly the three past-retention entries without a live run, each stating
    # its path and its size, and the dry run removed nothing.
    rows = _rows(dry)
    assert len(rows) == 3
    sizes = {row["path"]: row["size"] for row in rows}
    assert all(isinstance(size, int) for size in sizes.values())
    # The report tree and the run directory state the bytes they hold; a lock
    # file is an empty advisory handle, so its honest size is zero.
    assert sizes[str(raw_old)] == raw_old.stat().st_size > 0
    assert sizes[str(stores["dir_old"])] > 0
    assert sizes[str(stores["lock_old"])] == 0
    assert all(Path(row["path"]).exists() for row in rows)
    assert raw_old.is_file()
    assert stores["lock_old"].is_file()
    assert stores["dir_old"].is_dir()

    applied = gc(repo, apply=True)

    removed = [row for row in _rows(applied) if row["removed"]]
    assert len(removed) == 3
    assert not raw_old.is_file()
    assert not stores["lock_old"].exists()
    assert not stores["dir_old"].exists()
    # Only the raw report text is pruned: the sidecar and snapshot that a
    # coverage read depends on stay beside it.
    assert (report_old / plan_review._REVIEW_SIDECAR_NAME).is_file()
    assert (report_old / plan_review._REVIEW_SNAPSHOT_NAME).is_file()
    # Everything the live runs and the fresh entries hold is left in place.
    assert (stores["report_fresh"] / plan_review._REVIEW_REPORT_NAME).is_file()
    assert stores["report_live"].is_dir()
    assert stores["lock_fresh"].is_file()
    assert stores["lock_live"].is_file()
    assert stores["dir_fresh"].is_dir()
    assert stores["dir_live"].is_dir()


def test_a_live_run_holds_its_lock_and_run_directory(tmp_path, monkeypatch):
    """A run still named by a live pointer keeps its lock and directory even
    when both are far past retention."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    repo = repository(tmp_path)
    record_run(repo, "held-run")
    lock = write_lock("held-run", age_days=OLD_DAYS)
    directory = write_run_dir("held-run", age_days=OLD_DAYS)
    write_pointer("held-run")

    applied = gc(repo, apply=True)

    assert lock.is_file()
    assert directory.is_dir()
    assert all(row["path"] != str(lock) for row in applied["lock_files"])


def test_the_real_crew_home_is_untouched(tmp_path, monkeypatch):
    """Every store the sweep reaches is the fixture's own under RECKON_HOME."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    repo = repository(tmp_path)
    write_report_tree(repo, "pr-report-old", age_days=OLD_DAYS, committed=True)

    real_home = Path.home() / ".config" / "reckon"
    before = real_home.exists()
    gc(repo, apply=True)

    assert real_home.exists() == before
    assert str(reports_dir()).startswith(os.environ["RECKON_HOME"])


def test_the_queue_lock_is_never_offered(tmp_path, monkeypatch):
    """The queue's own ``queued-dispatch.lock`` is not a run's, so gc leaves it.

    A run named ``queued-dispatch`` sits in the ledger and its lock is far past
    retention, so every other condition the lock set tests is met: the name is
    the only thing that spares it, and dropping that exclusion offers and removes
    the queue's lock.
    """
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    repo = repository(tmp_path)
    record_run(repo, "queued-dispatch")
    queue_lock = write_lock("queued-dispatch", age_days=OLD_DAYS)

    applied = gc(repo, apply=True)

    assert queue_lock.is_file()
    assert all(row["path"] != str(queue_lock) for row in applied["lock_files"])


def test_a_lock_with_no_ledger_row_is_never_offered(tmp_path, monkeypatch):
    """gc offers a lock only once its run has a ledger row: a lock whose run has
    no row is left, because gc cannot prove that run has finished."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    repo = repository(tmp_path)
    stray = write_lock("no-ledger-row", age_days=OLD_DAYS)

    applied = gc(repo, apply=True)

    assert stray.is_file()
    assert all(row["path"] != str(stray) for row in applied["lock_files"])


def test_a_confined_run_offers_its_tree_while_it_is_live(tmp_path, monkeypatch):
    """``--run`` owns the named run's artifacts, so its own live pointer does not
    withhold its report tree; the same pointer withholds it from a whole sweep."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    repo = repository(tmp_path)
    tree = _worktree(repo, "confined-tree")
    write_general_tree("live-tree", age_days=OLD_DAYS)
    write_pointer("live-tree", worktree=tree)

    held_tree = str(reports_dir() / PROJECT / "live-tree")
    unconfined = gc(repo, apply=False)
    assert all(row["path"] != held_tree for row in unconfined["report_trees"])

    confined = gc(repo, apply=False, run="live-tree")
    offered = {
        row["path"]
        for row in confined["report_trees"]
        if row["run_id"] == "live-tree"
    }
    assert offered == {held_tree}


# ── The run directory is shared, so its ledger set is read across every mount ──


def repository_named(root: Path, name: str) -> Path:
    """A minimal committed checkout carrying project ``name``'s ledger."""
    repo = root / f"repo-{name}"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "seed.txt").write_text("seed\n")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "test: seed")
    (repo / "docs" / "state" / name).mkdir(parents=True)
    return repo


def register_mounts(tmp_path: Path, projects: dict[str, Path]) -> Path:
    """Write the mount registry the gc sweep reads, isolated to the fixture."""
    mounts = tmp_path / "config" / "mounts.json"
    mounts.parent.mkdir(parents=True, exist_ok=True)
    mounts.write_text(
        json.dumps({"mounts": {name: str(docs) for name, docs in projects.items()}}),
        encoding="utf-8",
    )
    return mounts


def record_run_in(project: str, repo: Path, run_id: str) -> None:
    ledger.append_run(
        project,
        ledger.build_record(run_id=run_id, plan="plan-a", gate="passed", node="node-a"),
        root=repo,
    )


def gc_for(repo: Path, project: str, *, apply: bool) -> dict:
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "gc",
            "--repo",
            str(repo),
            "--project",
            project,
            "--retention-days",
            str(RETENTION),
            *(["--apply"] if apply else []),
        ],
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_gc_keeps_a_peer_projects_run_directory(tmp_path, monkeypatch):
    """gc from project A offers only the past-retention run directory that no
    registered project's ledger holds, leaving a peer project's directory on
    disk even though A's own ledger carries no row for it."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    repo_a = repository_named(tmp_path, "project-a")
    repo_b = repository_named(tmp_path, "project-b")
    monkeypatch.setenv(
        "RECKON_MOUNTS_PATH",
        str(
            register_mounts(
                tmp_path,
                {"project-a": repo_a / "docs", "project-b": repo_b / "docs"},
            )
        ),
    )
    record_run_in("project-a", repo_a, "held-by-a")
    record_run_in("project-b", repo_b, "held-by-b")
    dir_a = write_run_dir("held-by-a", age_days=OLD_DAYS)
    dir_b = write_run_dir("held-by-b", age_days=OLD_DAYS)
    dir_none = write_run_dir("held-by-none", age_days=OLD_DAYS)

    dry = gc_for(repo_a, "project-a", apply=False)
    assert {row["path"] for row in dry["unledgered_run_directories"]} == {str(dir_none)}

    applied = gc_for(repo_a, "project-a", apply=True)
    assert {row["path"] for row in applied["unledgered_run_directories"]} == {
        str(dir_none)
    }
    assert not dir_none.exists()
    # A's own ledger row spares its directory; B's ledger row, read across the
    # mount, spares B's. The peer's run directory stays on disk.
    assert dir_a.is_dir()
    assert dir_b.is_dir()


def test_an_unreadable_peer_ledger_withholds_the_sweep(tmp_path, monkeypatch):
    """An unreadable registered ledger fails the unledgered sweep closed:
    nothing is removed, and every would-be row names the unreadable project."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    repo_a = repository_named(tmp_path, "project-a")
    repo_b = repository_named(tmp_path, "project-b")
    monkeypatch.setenv(
        "RECKON_MOUNTS_PATH",
        str(
            register_mounts(
                tmp_path,
                {"project-a": repo_a / "docs", "project-b": repo_b / "docs"},
            )
        ),
    )
    record_run_in("project-a", repo_a, "held-by-a")
    dir_a = write_run_dir("held-by-a", age_days=OLD_DAYS)
    record_run_in("project-b", repo_b, "held-by-b")
    dir_b = write_run_dir("held-by-b", age_days=OLD_DAYS)
    dir_none = write_run_dir("held-by-none", age_days=OLD_DAYS)
    # B's ledger is unreadable: a collection that is not list-valued.
    (repo_b / "docs" / "state" / "project-b" / "crew.json").write_text(
        json.dumps({"members": [], "runs": "corrupt", "holds": []}), encoding="utf-8"
    )

    dry = gc_for(repo_a, "project-a", apply=False)
    rows = {row["path"]: row for row in dry["unledgered_run_directories"]}
    # A's own directory is unambiguously ledgered, so it is not a candidate;
    # B's directory and the unledgered one are both withheld rather than offered.
    assert set(rows) == {str(dir_b), str(dir_none)}
    assert all("withheld" in row for row in rows.values())
    assert all(row["action"] == "withheld" for row in rows.values())
    assert all("project-b" in row["withheld"] for row in rows.values())

    applied = gc_for(repo_a, "project-a", apply=True)
    assert all(not row["removed"] for row in applied["unledgered_run_directories"])
    assert dir_a.is_dir()
    assert dir_b.is_dir()
    assert dir_none.is_dir()


def test_gc_spares_its_own_ledgered_run_directory_with_no_mount(tmp_path, monkeypatch):
    """A repository that runs workers without being a registered mount still
    spares its own ledgered run directory: the gc repository's own ledgered
    set is carried alongside the cross-mount set."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(tmp_path / "config" / "mounts.json"))
    repo = repository(tmp_path)
    record_run(repo, "own-ledgered")
    directory = write_run_dir("own-ledgered", age_days=OLD_DAYS)

    applied = gc(repo, apply=True)

    assert directory.is_dir()
    assert all(
        row["path"] != str(directory) for row in applied["unledgered_run_directories"]
    )
