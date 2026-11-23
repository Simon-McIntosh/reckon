"""An unchanged obligations sweep parses no plan and walks no state tree.

The watch producer derives every registered session's obligations on each
trigger. It used to re-parse every candidate plan, to read the section duties,
and recurse through the project's state tree statting every file under it, to
decide whether anything had moved. Both costs are gone: the sweep compares the
directories of the trees it watches, one stat per directory rather than one per
file, and it hands that reading to a plan-state cache that re-parses only a plan
whose file changed. A sweep over an unchanged project therefore parses no plan
and stats no tree file; a plan, state file or review that does change is still
seen on the next sweep, and the published obligations match those of a
from-scratch sweep, compared row for row.

Every sweep here is a full pass: the sweep's in-process memory is cleared and
the clock is advanced past both the stat-identity interval and the floor tick,
so neither base shortcut can answer the second sweep on the first one's behalf.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self

import pytest

from reckon import _store, file_memo, ledger
from reckon.crew import obligation_snapshot, review, runs, section_obligations

PROJECT = "sweep-fixture"
SESSION = "coordinator-fixture"
PLANS = ("plan-a", "plan-b", "plan-c", "plan-d")
STATE_DIRECTORIES = 200
FILES_PER_DIRECTORY = 5
#: The project's own control files the sweep resolves by name each trigger --
#: its ledger rows, its review index and the optional project config. A fixed
#: handful that does not grow with the state tree, and not "the tree" the sweep
#: used to walk.
CONTROL_PROBES = 16
#: The ledger's own rows directory, which the tree identity excludes by design.
CONTROL_DIRECTORIES = 1
ADVANCE = timedelta(seconds=180)
NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC)


def _git(tree: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(tree), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _plan_html(slug: str) -> str:
    declarations = json.dumps({"s1": "implementable"}).replace('"', "&quot;")
    return (
        "<!doctype html>\n"
        f'<meta name="docs-project" content="{PROJECT}">\n'
        '<meta name="reckon-type" content="plan">\n'
        f'<meta name="plan-slug" content="{slug}">\n'
        '<meta name="plan-status" content="active">\n'
        f'<meta name="plan-section-declarations" content="{declarations}">\n'
        '<h2 id="s1">section one</h2>\n'
    )


def _write_state_population(state_dir: Path) -> None:
    """At least a thousand state files, several to each watched directory."""
    archive = state_dir / "archive"
    archive.mkdir()
    for bucket in range(STATE_DIRECTORIES):
        directory = archive / f"bucket-{bucket:04d}"
        directory.mkdir()
        for slot in range(FILES_PER_DIRECTORY):
            (directory / f"record-{slot}.json").write_text(
                json.dumps({"bucket": bucket, "slot": slot}) + "\n", encoding="utf-8"
            )


@pytest.fixture()
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    file_memo.clear()
    _clear_plan_reads()
    obligation_snapshot._SWEEPS.clear()
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repository = tmp_path / "checkout"
    state_dir = repository / "docs" / "state" / PROJECT
    plans_dir = repository / "docs" / "plans"
    state_dir.mkdir(parents=True)
    plans_dir.mkdir(parents=True)
    (state_dir / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    for slug in PLANS:
        (plans_dir / f"{slug}.html").write_text(_plan_html(slug), encoding="utf-8")
    _write_state_population(state_dir)

    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    _git(repository, "config", "user.name", "fixture")
    _git(repository, "add", "--", "docs")
    _git(repository, "commit", "-q", "-m", "chore: seed the sweep fixture")
    head = _git(repository, "rev-parse", "HEAD")

    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )

    run_ids: dict[str, str] = {}
    for index, slug in enumerate(PLANS):
        run_id = f"r-merged-{index}"
        run_ids[slug] = run_id
        record = ledger.build_record(
            run_id=run_id,
            plan=slug,
            section="s1",
            node=run_id,
            role="implement",
            gate="passed",
            commits=[head],
            completed_at="2026-11-20T10:00:00Z",
        )
        record.update(session=SESSION, promoted_revision=head)
        ledger.append_run(PROJECT, record, root=repository)
        runs._write_json(
            runs.pointer_path(run_id),
            {
                "run_id": run_id,
                "project": PROJECT,
                "session": SESSION,
                "repo": str(repository),
                "worktree": str(repository),
                "base_sha": head,
                "process_alive": False,
                "phase": "working",
                "launch": "in-harness",
                "role": "implement",
                "manifest_path": str(config_home / "manifests" / f"{run_id}.md"),
                "node": {"id": run_id, "plan": slug, "section": "s1"},
            },
        )
    return {
        "home": config_home,
        "repo": repository,
        "state": state_dir,
        "plans": plans_dir,
        "head": head,
        "run_ids": run_ids,
    }


def _sweep(fleet: dict[str, Any], now: datetime) -> list[Path]:
    """Drive exactly one full sweep: no carried memory, the clock advanced."""
    obligation_snapshot._SWEEPS.clear()
    return obligation_snapshot.sweep(
        PROJECT,
        sessions=[SESSION],
        producer={
            "pid": 1,
            "pid_start_time": "1",
            "started_at": None,
            "code_stamp": "sweep-fixture",
        },
        stream_offset=0,
        transition_fired=False,
        state_dirs=[fleet["state"], fleet["plans"]],
        now=now,
    )


def _obligations() -> list[dict[str, Any]]:
    document = obligation_snapshot.read_snapshot(PROJECT, SESSION)
    assert document is not None
    return document["obligations"]


def _clear_plan_reads() -> None:
    clear = getattr(section_obligations, "clear_plan_reads", None)
    if clear is not None:
        clear()


def _cold_obligations(fleet: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """The questions a from-scratch process would publish over this state."""
    obligation_snapshot._SWEEPS.clear()
    _clear_plan_reads()
    file_memo.clear()
    _sweep(fleet, now)
    return _obligations()


class _StatCounter:
    """Count ``os.stat`` calls under the watched roots."""

    def __init__(self, roots: list[Path]) -> None:
        self._roots = [str(root) for root in roots]
        self.paths: list[str] = []

    def __enter__(self) -> Self:
        self._real = os.stat
        counter = self

        def counting(path: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                name = os.fspath(path)
            except TypeError:
                name = str(path)
            if isinstance(name, str) and any(
                name.startswith(root) for root in counter._roots
            ):
                counter.paths.append(name)
            return counter._real(path, *args, **kwargs)

        os.stat = counting
        return self

    def __exit__(self, *exc: object) -> None:
        os.stat = self._real


class _ParseCounter:
    """Count whole-plan parses reached through the store."""

    def __enter__(self) -> Self:
        self._real = _store.read_plan
        self.plans: list[str] = []
        counter = self

        def counting(*args: Any, **kwargs: Any) -> Any:
            slug = args[1] if len(args) > 1 else kwargs.get("slug")
            counter.plans.append(str(slug))
            return counter._real(*args, **kwargs)

        _store.read_plan = counting
        return self

    def __exit__(self, *exc: object) -> None:
        _store.read_plan = self._real


def _watched_tree(fleet: dict[str, Any]) -> dict[str, tuple[int, int]]:
    """The directories a sweep watches, from the sweep's own reading.

    A revision without the directory-identity reader still gets a watched set,
    computed here by walking the same two roots, so this file runs against the
    behaviour it is written to replace and fails there rather than erroring out.
    """
    roots = [fleet["state"], fleet["plans"]]
    identity = getattr(obligation_snapshot, "_directory_identity", None)
    if identity is not None:
        return identity(roots)
    watched: dict[str, tuple[int, int]] = {}
    for root in roots:
        for directory, children, _files in os.walk(root):
            children[:] = [name for name in children if name != "runs"]
            metadata = os.stat(directory)
            watched[directory] = (metadata.st_ino, metadata.st_mtime_ns)
    return watched


def _store_review(fleet: dict[str, Any], run_id: str) -> None:
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review.REVIEW_DIMENSIONS
    )
    stored = review.parse_review(emitted)
    stored.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
            "reviewed_base_sha": fleet["head"],
            "reviewed_head_sha": fleet["head"],
            "findings_ok": True,
            "findings": [],
        }
    )
    review.store_review(stored)


def test_an_unchanged_sweep_parses_no_plan_and_stats_only_directories(
    fleet: dict[str, Any],
) -> None:
    """The second of two full sweeps reads no plan and no tree file."""
    _sweep(fleet, NOW)
    watched_tree = _watched_tree(fleet)
    watched = len(watched_tree)
    assert watched >= STATE_DIRECTORIES

    with (
        _ParseCounter() as parses,
        _StatCounter([fleet["state"], fleet["plans"]]) as stats,
    ):
        written = _sweep(fleet, NOW + ADVANCE)

    assert written, "a due sweep with no changed input still publishes the floor"

    stat_paths = set(stats.paths)
    population_files = [
        path for path in stat_paths if path.endswith(".json") and "/archive/" in path
    ]
    directories = {path for path in stat_paths if os.path.isdir(path)}
    probes = stat_paths - directories
    problems: list[str] = []
    if parses.plans:
        problems.append(f"an unchanged sweep parsed plans: {parses.plans}")
    if population_files:
        problems.append(
            f"an unchanged sweep stat'ed {len(population_files)} files of the state"
            f" population: {sorted(population_files)[:8]}"
        )
    # The ledger keeps its own rows in a directory the tree identity excludes,
    # so the sweep may stat that one control directory beyond the watched tree.
    outside = sorted(directories - set(watched_tree))
    if len(outside) > CONTROL_DIRECTORIES:
        problems.append(
            f"an unchanged sweep stat'ed directories outside its tree: {outside}"
        )
    if len(probes) > CONTROL_PROBES:
        problems.append(
            f"an unchanged sweep stat'ed more than its fixed control files: {sorted(probes)}"
        )
    assert problems == [], "\n".join(problems)
    assert _obligations() == _cold_obligations(fleet, NOW + ADVANCE)


def test_one_changed_plan_reparses_that_plan_alone(fleet: dict[str, Any]) -> None:
    """A plan replaced on disk invalidates its parse and no neighbour's."""
    _sweep(fleet, NOW)
    changed = fleet["plans"] / f"{PLANS[1]}.html"
    replacement = changed.with_suffix(".tmp")
    replacement.write_text(
        _plan_html(PLANS[1]).replace("section one", "section one revised"),
        encoding="utf-8",
    )
    os.replace(replacement, changed)

    with _ParseCounter() as parses:
        written = _sweep(fleet, NOW + ADVANCE)

    assert written
    assert parses.plans == [PLANS[1]], (
        "a sweep after one plan changed parsed:",
        parses.plans,
    )
    assert _obligations() == _cold_obligations(fleet, NOW + ADVANCE)


def test_a_state_file_change_is_published_like_a_cold_sweep(
    fleet: dict[str, Any],
) -> None:
    """A state-tree change republishes the same rows a cold sweep derives."""
    assert _sweep(fleet, NOW)

    record = fleet["state"] / "archive" / "bucket-0000" / "record-0.json"
    payload = json.loads(record.read_text(encoding="utf-8"))
    payload["slot"] = "changed"
    record.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    assert _sweep(fleet, NOW + ADVANCE), "a changed sweep published nothing"
    warm = _obligations()
    assert warm == _cold_obligations(fleet, NOW + ADVANCE)


def test_an_added_review_record_is_published_like_a_cold_sweep(
    fleet: dict[str, Any],
) -> None:
    """A review record added between sweeps is seen, matching a cold sweep."""
    assert _sweep(fleet, NOW)
    _store_review(fleet, fleet["run_ids"][PLANS[0]])

    assert _sweep(fleet, NOW + ADVANCE), (
        "a sweep after a review was added published nothing"
    )
    assert _obligations() == _cold_obligations(fleet, NOW + ADVANCE)
