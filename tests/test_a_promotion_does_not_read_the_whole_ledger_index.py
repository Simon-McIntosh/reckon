"""A promotion decodes the ledger index once per read key before it appends.

Promotion reads the ledger several times before it writes its row: once to find
an existing row, once more inside the landing checks, and once per project from
the tree-boundary scan. Each of those reads rebuilt and decoded the whole index,
so the pre-append cost of a promotion grew with the ledger — the one stage of a
promotion that does.

The reuse region makes every read inside it share one decode per read key, and
it is closed before the append so a read after the append decodes afresh and
sees the row the promotion just wrote. These tests drive one real
``crew complete --gate not-run`` over a 2,000-row ledger and count the decodes
each read key costs before the append.

Nothing here touches the operator's store: the crew home, the ledger and the
checkout are all built under the test's own temporary directory.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner

from reckon import _plan_html, ledger
from reckon import cli as cli_module
from reckon.crew.runs import _write_json, pointer_path, run_dir
from tests.helpers.git import git as _git

PROJECT = "proj"
PLAN = "plan-a"
RUN_ID = "r-20261111T070000000000-node-a"
ROW_COUNT = 2000
GATE_COMMAND = f"{shlex.quote(sys.executable)} -c pass"


def _synthetic_row(position: int) -> dict:
    """One completed run row, shaped like a promoted implement run."""
    return {
        "run_id": f"r-20260101T{position // 3600 % 24:02d}{position // 60 % 60:02d}"
        f"{position % 60:02d}000000-fixture-{position:04d}",
        "project": PROJECT,
        "node": f"fixture-node-{position:04d}",
        "plan": PLAN,
        "role": "implement",
        "gate": "passed",
        "status": "complete",
        "session_id": f"session-{position:04d}",
        "dispatched_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:05:00Z",
        "wall_seconds": 300.0,
    }


def _write_plan(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(
        _plan_html.write_state(
            bare,
            {
                "type": "plan",
                "slug": PLAN,
                "title": "Plan A",
                "status": "active",
                "version": 0,
                "comments": {},
            },
        ),
        encoding="utf-8",
    )


def _build_checkout(root: Path) -> Path:
    """A committed checkout a promotion can land its record into."""
    _write_plan(root / "docs" / "plans" / f"{PLAN}.html")
    (root / "docs" / "state" / PROJECT).mkdir(parents=True, exist_ok=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("config", "commit.gpgsign", "false"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "docs", "seed.txt")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    return root


def _seed_ledger(root: Path) -> None:
    """Write a ledger of ``ROW_COUNT`` rows, then warm its index with one read.

    The warm read is what makes every later read a decode of the stored index
    rather than a rebuild of it, which is the cost this test is about.
    """
    ledger.write(
        PROJECT,
        {
            "members": [],
            "runs": [_synthetic_row(position) for position in range(ROW_COUNT)],
            "holds": [],
        },
        0,
        root=root,
    )
    rows, _version = ledger.load(PROJECT, root=root)
    assert len(rows["runs"]) == ROW_COUNT


def _write_completed_run(root: Path, tmp_path: Path) -> None:
    """A completed report-only run, as the live pointer promotion reads."""
    manifest = tmp_path / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: node-a\n"
        "status: complete\n"
        "changed_paths: none (the sole deliverable is the report)\n"
        f"tests: {GATE_COMMAND}\n",
        encoding="utf-8",
    )
    run_dir(RUN_ID).mkdir(parents=True, exist_ok=True)
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(root),
            "worktree": str(root),
            "base_sha": _git(root, "rev-parse", "HEAD"),
            "launch": "in-harness",
            "role": "implement",
            "backend": "native",
            "created_at": "2026-11-11T06:59:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "s2",
                "time_budget": "25m",
                "write_paths": [],
            },
        },
    )


class _DecodeCounter:
    """Count full-index decodes per read key, and when they happened.

    A read key is the index file a read decodes plus the projection it asked
    for: two reads of the same project's ledger with different ``picker_only``
    or ``headers_only`` arguments are different decodes by design, and the
    region shares a decode within a key rather than across keys.
    """

    def __init__(self) -> None:
        self.before_append: dict[tuple, int] = {}
        self.after_append: dict[tuple, int] = {}
        self.appended = False
        self.control_decodes = 0

    def key(self, connection, options: dict) -> tuple:
        database = connection.execute("PRAGMA database_list").fetchone()[2]
        return (
            str(database),
            bool(options.get("headers_only")),
            bool(options.get("picker_only")),
            options.get("activity_since"),
        )

    def record(self, key: tuple) -> None:
        bucket = self.after_append if self.appended else self.before_append
        bucket[key] = bucket.get(key, 0) + 1

    def before_counts(self) -> dict[tuple, int]:
        return dict(self.before_append)


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.delenv("RECKON_RUN_ID", raising=False)
    return config_home


@pytest.fixture()
def repository(tmp_path: Path, home: Path) -> Path:
    root = _build_checkout(tmp_path / "repo")
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _drive_one_promotion(repository: Path, tmp_path: Path) -> _DecodeCounter:
    """Run one ``crew complete --gate not-run``, counting index decodes."""
    counter = _DecodeCounter()
    original_payload = ledger._index_payload
    original_append = ledger.append_run

    def counting_payload(connection, meta, **options):
        counter.record(counter.key(connection, options))
        return original_payload(connection, meta, **options)

    def tracking_append(*args, **kwargs):
        result = original_append(*args, **kwargs)
        counter.appended = True
        return result

    with (
        mock.patch.object(ledger, "_index_payload", counting_payload),
        mock.patch.object(ledger, "append_run", tracking_append),
    ):
        observed, _version = ledger.load(PROJECT, root=repository)
        assert len(observed["runs"]) == ROW_COUNT
        counter.control_decodes = sum(counter.before_append.values())
        assert counter.control_decodes > 0
        counter.before_append.clear()
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
                str(repository),
            ],
        )
    assert result.exit_code == 0, result.output
    return counter


def _seed(root: Path, tmp_path: Path) -> None:
    _seed_ledger(root)
    _write_completed_run(root, tmp_path)


def test_a_promotion_decodes_each_index_key_once_before_its_append(
    repository: Path, tmp_path: Path
) -> None:
    """Every read key before the append costs one decode, however many reads."""
    _seed(repository, tmp_path)

    counter = _drive_one_promotion(repository, tmp_path)

    # A known history read proves the instrument sees decodes even when the
    # operational projection makes the promotion's history decode count zero.
    assert counter.control_decodes > 0
    repeated = {key: count for key, count in counter.before_append.items() if count > 1}
    assert repeated == {}, (
        "a read key was decoded more than once before the append: "
        f"{repeated} of {counter.before_append}"
    )


def test_a_read_after_the_append_sees_the_promoted_row(
    repository: Path, tmp_path: Path
) -> None:
    """The region closes before the append, so the new row is not hidden."""
    _seed(repository, tmp_path)

    _drive_one_promotion(repository, tmp_path)

    rows, _version = ledger.load(PROJECT, root=repository)
    assert RUN_ID in {str(row.get("run_id") or "") for row in rows["runs"]}
    assert ledger.run_path(PROJECT, RUN_ID, repository).is_file()


def measure_before_append_decodes(workspace: Path) -> dict:
    """The count this measure reports, over a fixture built in ``workspace``.

    Imported by the base/head measurement harness so both arms drive the same
    fixture through whichever revision of the promotion code is on the path.
    """
    root = _build_checkout(workspace / "repo")
    config_home = workspace / "config"
    config_home.mkdir()
    previous = {key: os.environ.get(key) for key in ("RECKON_HOME", "RECKON_RUN_ID")}
    os.environ["RECKON_HOME"] = str(config_home)
    os.environ.pop("RECKON_RUN_ID", None)
    try:
        (config_home / "mounts.json").write_text(
            json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
        )
        _seed(root, workspace)
        counter = _drive_one_promotion(root, workspace)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return {
        "rows": ROW_COUNT,
        "before_append": {
            str(key): count for key, count in counter.before_append.items()
        },
        "after_append": {
            str(key): count for key, count in counter.after_append.items()
        },
        "max_before_append": max(counter.before_append.values(), default=0),
        "row_after_append": RUN_ID
        in {
            str(row.get("run_id") or "")
            for row in ledger.load(PROJECT, root=root)[0]["runs"]
        },
    }
