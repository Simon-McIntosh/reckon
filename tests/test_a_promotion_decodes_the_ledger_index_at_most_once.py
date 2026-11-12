"""A promotion decodes the ledger index at most once before it appends.

A promotion reads the ledger several times before it writes its row — to find
an existing row, inside the landing checks, and again from the tree-boundary
scan. The reuse region serves every one of those reads from a single decode per
read key, and it is closed before the append so a read afterwards decodes
afresh. The counting measure for the implement landing lives beside this file;
what it does not cover is the other half of the pair a promotion can be, and
the freshness a region must not cost.

These tests drive the two promotions a node can make — one review run and one
implement landing — and count the index decodes each read key costs before its
append. They then hold the region to its other obligation: a read after the
append returns the promoted row *and* a row a second process appended in
between, because a region that outlived the append would hide the peer's row.
Last, they check that the fleet-state reading and the landing preconditions
answer the same values through the region as they do without it, so the decode
the region saves is not bought with a different answer.

Nothing here touches the operator's store: the crew home, the ledger and the
checkout are all built under the test's own temporary directory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import ledger
from reckon.crew import promotion_checks, recovery_stream
from reckon.crew.runs import _write_json, pointer_path
from tests import test_a_promotion_does_not_read_the_whole_ledger_index as fixture

PROJECT = fixture.PROJECT
PLAN = fixture.PLAN
ROW_COUNT = fixture.ROW_COUNT
REVIEW_RUN_ID = "r-20261111T070100000000-review-a"
PEER_RUN_ID = "r-20261111T070200000000-peer-b"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.delenv("RECKON_RUN_ID", raising=False)
    return config_home


@pytest.fixture()
def repository(tmp_path: Path, home: Path) -> Path:
    root = fixture._build_checkout(tmp_path / "repo")
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _write_role_run(root: Path, home: Path, run_id: str, role: str) -> None:
    """A completed report-only run of ``role``, as the live pointer reads it."""
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: node-a\n"
        "status: complete\n"
        "changed_paths: none (the sole deliverable is the report)\n"
        f"tests: {fixture.GATE_COMMAND}\n",
        encoding="utf-8",
    )
    from reckon.crew.runs import run_dir

    run_dir(run_id).mkdir(parents=True, exist_ok=True)
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(root),
            "worktree": str(root),
            "base_sha": fixture._git(root, "rev-parse", "HEAD"),
            "launch": "in-harness",
            "role": role,
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


def _peer_append(project: str, root: Path, run_id: str) -> subprocess.CompletedProcess:
    """Append one row to ``root``'s ledger from a second interpreter.

    The write is made by another process on purpose: a row appended in this one
    could be served from an in-process cache, which is exactly the freshness a
    read after the append has to prove it does not depend on.
    """
    record = dict(fixture._synthetic_row(ROW_COUNT + 1), run_id=run_id)
    script = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from reckon import ledger\n"
        "project, root, run_id, payload = sys.argv[1:5]\n"
        "ledger.append_run(\n"
        "    project,\n"
        "    json.loads(payload),\n"
        "    root=Path(root),\n"
        "    allow_create=True,\n"
        ")\n"
        "print('appended', run_id)\n"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    return subprocess.run(
        [sys.executable, "-c", script, project, str(root), run_id, json.dumps(record)],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
        timeout=120,
    )


def _drive_promotion(
    repository: Path,
    run_id: str,
    counter: fixture._DecodeCounter,
    *,
    after_append=None,
):
    """Run one ``crew complete --gate not-run``, counting index decodes."""
    original_payload = ledger._index_payload
    original_append = ledger.append_run

    def counting_payload(connection, meta, **options):
        counter.record(counter.key(connection, options))
        return original_payload(connection, meta, **options)

    def tracking_append(*args, **kwargs):
        result = original_append(*args, **kwargs)
        counter.appended = True
        if after_append is not None:
            after_append()
        return result

    with (
        mock.patch.object(ledger, "_index_payload", counting_payload),
        mock.patch.object(ledger, "append_run", tracking_append),
    ):
        return CliRunner().invoke(
            cli_module.main,
            [
                "crew",
                "complete",
                "--run",
                run_id,
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


def _assert_one_decode_per_key(counter: fixture._DecodeCounter) -> None:
    # The counter is shown to see decodes before an absence is reported: a
    # promotion that made no read key at all would pass the check below.
    assert counter.before_append, "no index decode was recorded before the append"
    repeated = {key: count for key, count in counter.before_append.items() if count > 1}
    assert repeated == {}, (
        "a read key was decoded more than once before the append: "
        f"{repeated} of {counter.before_append}"
    )


def test_a_review_promotion_decodes_each_index_key_once_before_its_append(
    repository: Path, tmp_path: Path
) -> None:
    """A review run's promotion costs one decode per read key before its append."""
    fixture._seed_ledger(repository)
    _write_role_run(repository, tmp_path, REVIEW_RUN_ID, "review")
    counter = fixture._DecodeCounter()

    result = _drive_promotion(repository, REVIEW_RUN_ID, counter)

    assert result.exit_code == 0, result.output
    _assert_one_decode_per_key(counter)


def test_an_implement_landing_decodes_each_index_key_once_before_its_append(
    repository: Path, tmp_path: Path
) -> None:
    """An implement landing costs one decode per read key before its append."""
    fixture._seed(repository, tmp_path)
    counter = fixture._DecodeCounter()

    result = _drive_promotion(repository, fixture.RUN_ID, counter)

    assert result.exit_code == 0, result.output
    _assert_one_decode_per_key(counter)


def test_a_read_after_the_append_sees_the_promoted_row_and_a_peer_row(
    repository: Path, tmp_path: Path
) -> None:
    """The region is closed before the append, so a peer's row is not hidden."""
    fixture._seed_ledger(repository)
    _write_role_run(repository, tmp_path, REVIEW_RUN_ID, "review")
    counter = fixture._DecodeCounter()
    appended: list[subprocess.CompletedProcess] = []

    def append_peer() -> None:
        appended.append(_peer_append(PROJECT, repository, PEER_RUN_ID))

    result = _drive_promotion(
        repository, REVIEW_RUN_ID, counter, after_append=append_peer
    )

    assert result.exit_code == 0, result.output
    assert appended and appended[0].returncode == 0, (
        appended[0].stderr if appended else "the peer appended nothing"
    )
    rows, _version = ledger.load(PROJECT, root=repository)
    seen = {str(row.get("run_id") or "") for row in rows["runs"]}
    assert REVIEW_RUN_ID in seen, "the read after the append missed the promoted row"
    assert PEER_RUN_ID in seen, "the read after the append missed the peer's row"


def test_the_fleet_reading_and_landing_preconditions_match_without_the_region(
    repository: Path, tmp_path: Path, monkeypatch
) -> None:
    """Serving reads from one region answers what the unshared reads answer."""
    fixture._seed_ledger(repository)
    _write_role_run(repository, tmp_path, REVIEW_RUN_ID, "review")
    record = json.loads(pointer_path(REVIEW_RUN_ID).read_text(encoding="utf-8"))
    bucket = int(recovery_stream._utc_seconds() // 300)

    def landing() -> str:
        return json.dumps(
            promotion_checks._landing_preconditions(
                REVIEW_RUN_ID,
                record,
                checkout=repository,
                ledger_root=repository,
                commits=[],
                gate="not-run",
                failure_classification="",
                no_impl_change="",
                plan_link="",
                unplanned_reason="",
                boundary_waiver="",
                negative_control_waiver=None,
                accepted_paths=None,
                gate_check=None,
                require_gate_check=False,
            ),
            sort_keys=True,
            default=str,
        )

    def fleet() -> str:
        recovery_stream._historical_reference_rate.cache_clear()
        return json.dumps(
            recovery_stream._historical_reference_rate(
                PROJECT, "native", "a-model", bucket
            )
        )

    def unshared(reader) -> str:
        recovery_stream._historical_reference_rate.cache_clear()
        return reader()

    with ledger.index_reuse():
        shared_landing = landing()
        shared_fleet = fleet()
    alone_landing = unshared(landing)
    alone_fleet = unshared(fleet)

    assert shared_landing, "the landing preconditions answered nothing"
    assert shared_landing == alone_landing
    assert shared_fleet == alone_fleet
