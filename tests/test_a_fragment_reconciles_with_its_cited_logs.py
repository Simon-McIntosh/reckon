"""A fragment is reconciled with the logs its manifest cites.

An evidence record that quotes a figure no run produced passed every check in
the delivery path, so the comparison lives in
:mod:`reckon.crew.fragment_reconcile` and runs inside ``crew check --manifest``.
These cases hold each of its three parts to a fragment that only that part
refuses: a figure its log recorded, a fragment naming no revision, a fragment
quoting a figure its log never recorded, a figure standing too far from the
revision it belongs to, a malformed arm record, and a log the manifest names but
the run directory does not hold. Every tree here is synthesised under
``tmp_path``; no case reads a real run directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import reports
from reckon.crew.fragment_reconcile import (
    NEARBY_CHARACTERS,
    ManifestUnreadableError,
    reconcile,
)
from reckon.crew.node import TaskNode

BASE = "1" * 40
HEAD = "2" * 40
BASE_SUMMARY = "58 passed in 43.20s"
AFTER_SUMMARY = "58 passed in 36.19s"


def _log(path: Path, revision: str, summary: str) -> Path:
    path.write_text(
        f"# revision: {revision}\n# tree: /tmp/tree\n{summary}\nEXIT=0\n",
        encoding="utf-8",
    )
    return path


def _arm(revision: str, log: Path) -> str:
    return json.dumps(
        {
            "revision": revision,
            "command": "pytest -q tests",
            "exit_status": 0,
            "log_path": str(log),
            "completed": True,
            "failure_count": 0,
            "failure_ids": [],
        }
    )


def _manifest(base_log: Path, after_log: Path) -> str:
    return (
        "status: complete\n"
        "node: example\n"
        f"baseline_suite: {_arm(BASE, base_log)}\n"
        f"after_suite: {_arm(HEAD, after_log)}\n"
        "test_logs: none\n"
    )


def _logs(tmp_path: Path) -> tuple[Path, Path, str]:
    base_log = _log(tmp_path / "base.log", BASE, BASE_SUMMARY)
    after_log = _log(tmp_path / "after.log", HEAD, AFTER_SUMMARY)
    return base_log, after_log, _manifest(base_log, after_log)


def _fragment(*lines: str) -> str:
    body = "".join(f"<p>{line}</p>\n" for line in lines)
    return f"<section>\n{body}</section>\n"


def test_a_fragment_whose_figures_its_logs_record_reconciles(tmp_path: Path) -> None:
    """Every quoted figure stands beside the revision its log recorded."""
    _base, _after, manifest = _logs(tmp_path)
    fragment = _fragment(
        f"Base <code>{BASE}</code>",
        BASE_SUMMARY,
        f"Head <code>{HEAD}</code>",
        AFTER_SUMMARY,
    )

    assert reconcile(fragment, manifest) == []


def test_a_fragment_naming_no_revision_is_reported(tmp_path: Path) -> None:
    """A cited log's revision must be named, so a figure has an owner."""
    _base, _after, manifest = _logs(tmp_path)
    fragment = _fragment(BASE_SUMMARY, f"Head <code>{HEAD}</code>", AFTER_SUMMARY)

    failures = reconcile(fragment, manifest)

    assert any(BASE[:12] in failure for failure in failures)


def test_a_figure_its_log_never_recorded_is_reported(tmp_path: Path) -> None:
    """A stale timing cannot outlive the arm that recorded another one."""
    _base, _after, manifest = _logs(tmp_path)
    fragment = _fragment(
        f"Base <code>{BASE}</code>",
        "58 passed in 9.99s",
        f"Head <code>{HEAD}</code>",
        AFTER_SUMMARY,
    )

    failures = reconcile(fragment, manifest)

    assert any(
        "quotes no" in failure and BASE_SUMMARY in failure for failure in failures
    )


def test_a_figure_far_from_its_revision_is_reported(tmp_path: Path) -> None:
    """A figure stated pages away from its revision belongs to no arm here."""
    _base, _after, manifest = _logs(tmp_path)
    filler = "filler " * ((NEARBY_CHARACTERS // 7) + 60)
    fragment = _fragment(
        f"Base <code>{BASE}</code>",
        BASE_SUMMARY,
        f"Head <code>{HEAD}</code>",
        filler,
        AFTER_SUMMARY,
    )

    failures = reconcile(fragment, manifest)

    assert any(
        AFTER_SUMMARY in failure and "not stated beside" in failure
        for failure in failures
    )


def test_a_malformed_arm_is_refused_by_name(tmp_path: Path) -> None:
    """A broken arm record names itself instead of crashing the check."""
    base_log, after_log, _manifest_text = _logs(tmp_path)
    manifest = (
        "status: complete\n"
        "node: example\n"
        'baseline_suite: {"revision": "' + BASE + '", "command": "pytest\n'
        '", "exit_status: 0}\n'
        f"after_suite: {_arm(HEAD, after_log)}\n"
        f"test_logs: {base_log}\n"
    )

    with pytest.raises(ManifestUnreadableError, match="baseline_suite"):
        reconcile(_fragment(str(base_log)), manifest)


def test_an_absent_cited_log_is_reported(tmp_path: Path) -> None:
    """A citation to a log the run directory does not hold is a failure."""
    base_log = _log(tmp_path / "base.log", BASE, BASE_SUMMARY)
    missing = tmp_path / "missing.log"
    manifest = _manifest(base_log, missing)
    fragment = _fragment(f"Base <code>{BASE}</code>", BASE_SUMMARY)

    failures = reconcile(fragment, manifest)

    assert any(str(missing) in failure for failure in failures)


def test_the_manifest_check_reports_a_record_its_logs_contradict(
    tmp_path: Path,
) -> None:
    """The delivery path reads the record, not only a reader who runs it."""
    _base, _after, manifest = _logs(tmp_path)
    plan = "example-plan"
    node_id = "example-node"
    record = tmp_path / "docs" / "evidence" / "fragments" / plan / f"{node_id}.html"
    record.parent.mkdir(parents=True)
    record.write_text(
        _fragment(f"Base <code>{BASE}</code>", "58 passed in 9.99s"),
        encoding="utf-8",
    )
    node = TaskNode(id=node_id, goal="read it", plan=plan, spec_level="guided")

    findings = reports.audit_manifest(manifest, node, worktree=tmp_path)["findings"]

    assert any("does not reconcile" in finding for finding in findings)


def test_a_manifest_written_as_json_is_read_like_any_other(tmp_path: Path) -> None:
    """The arms are the typed objects the manifest reader returns, not a scan."""
    base_log = _log(tmp_path / "base.log", BASE, BASE_SUMMARY)
    after_log = _log(tmp_path / "after.log", HEAD, AFTER_SUMMARY)
    manifest = json.dumps(
        {
            "status": "complete",
            "node": "example",
            "baseline_suite": json.loads(_arm(BASE, base_log)),
            "after_suite": json.loads(_arm(HEAD, after_log)),
            "test_logs": [],
        }
    )
    fragment = _fragment(
        f"Base <code>{BASE}</code>",
        BASE_SUMMARY,
        f"Head <code>{HEAD}</code>",
        AFTER_SUMMARY,
    )

    assert reconcile(fragment, manifest) == []
