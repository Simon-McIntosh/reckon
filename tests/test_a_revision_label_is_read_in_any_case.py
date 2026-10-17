"""The gate-log audit reads a revision label in any case.

The manifest audit holds each suite arm's gate log against the revision that
arm records, finding it on the log's first line behind a label the capture
scripts write. The label was matched case-sensitively, so a first line reading
``REV=<sha>`` or ``Revision: <sha>`` named no revision: on 2026-10-17 the
whole-suite worker ``r-20261017T141450191818`` met this and rewrote its own gate
logs' first lines after the run, lowercasing ``REV=`` to ``rev=`` with a script
so the audit would pass. The revision value was unchanged, but a log edited
after the fact is no longer the capture it claims to be.

These cases pin the reader to the label in any case while leaving the label
itself required: ``REV=``, ``Revision:`` and ``rev=`` are each read, and a first
line naming only a tree path with hex digits still names no revision.

Every path here is a fixture under ``tmp_path``; nothing touches the operator's
own store.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew.reports import audit_manifest

BASE_REVISION = "1f0c5a7d94e2b6839c4d5e6f708192a3b4c5d6e7"
HEAD_REVISION = "9a8b7c6d5e4f3021122334455667788990aabbcc"


def _arm_manifest(
    manifest_path: Path,
    *,
    base_revision: str,
    head_revision: str,
    base_log: str,
    head_log: str,
) -> str:
    """A manifest whose two suite arms cite their logs, as the template writes them."""
    baseline = {
        "revision": base_revision,
        "command": "pytest -q",
        "exit_status": 0,
        "log_path": base_log,
        "completed": True,
        "failure_count": 0,
        "failure_ids": [],
    }
    after = dict(baseline, revision=head_revision, log_path=head_log)
    body = [
        "node: revision-label-fixture",
        "status: complete",
        f"commits: {head_revision}",
        "changed_paths: reckon/crew/reports.py",
        "tests: pytest tests/example.py -> 1 passed",
        "baseline_suite: " + json.dumps(baseline),
        "after_suite: " + json.dumps(after),
    ]
    manifest_path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return manifest_path.read_text(encoding="utf-8")


def _log(path: Path, first_line: str) -> Path:
    path.write_text(f"{first_line}\n1 passed\nEXIT=0\n", encoding="utf-8")
    return path


def _findings_naming(body: str, manifest_path: Path, head_log: Path) -> list[str]:
    audit = audit_manifest(body, manifest_path=manifest_path)
    return [finding for finding in audit["findings"] if str(head_log) in finding]


@pytest.mark.parametrize(
    "head_first_line",
    [
        f"REV={HEAD_REVISION} tree=/tmp/head",
        f"Revision: {HEAD_REVISION} tree=/tmp/head",
        f"rev={HEAD_REVISION} tree=/tmp/head",
        f"# arm=head REVISION={HEAD_REVISION} tree=/tmp/head",
    ],
    ids=["upper-equals", "title-colon", "lower-equals", "upper-in-comment"],
)
def test_a_gate_log_label_is_read_in_any_case(
    tmp_path: Path, head_first_line: str
) -> None:
    manifest_path = tmp_path / "manifest.md"
    _log(tmp_path / "base-gate.log", f"rev={BASE_REVISION} tree=/tmp/base label=base")
    head_log = _log(tmp_path / "head-gate.log", head_first_line)
    body = _arm_manifest(
        manifest_path,
        base_revision=BASE_REVISION,
        head_revision=HEAD_REVISION,
        base_log="base-gate.log",
        head_log="head-gate.log",
    )

    assert _findings_naming(body, manifest_path, head_log) == []


def test_a_tree_path_with_hex_digits_names_no_revision(tmp_path: Path) -> None:
    """The label is still required: a bare hex run is not read as a revision."""
    manifest_path = tmp_path / "manifest.md"
    _log(tmp_path / "base-gate.log", f"rev={BASE_REVISION} tree=/tmp/base")
    head_log = _log(
        tmp_path / "head-gate.log",
        f"tree=/scratch/runs/{HEAD_REVISION} source=/srv/repo",
    )
    body = _arm_manifest(
        manifest_path,
        base_revision=BASE_REVISION,
        head_revision=HEAD_REVISION,
        base_log="base-gate.log",
        head_log="head-gate.log",
    )

    findings = _findings_naming(body, manifest_path, head_log)

    assert findings, audit_manifest(body, manifest_path=manifest_path)["findings"]
    assert any("revision" in finding for finding in findings)
