"""Every stored finding is readable by the repository path it names.

A review records its findings against paths, but the store keys records by the
run or plan they are about, so a recurring finding is only findable by reading
every record. The reader this holds groups a project's committed findings by
the repository-relative path they name and counts the distinct reviews naming
each path, keeping a path outside the repository — a report, a manifest — as an
artefact that adds no recurrence. Findings of every severity are read whole: a
follow-on is the row most easily lost, so the guard against dropping it is the
mutation this test's negative control applies.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import mcp
from reckon.cli import main as cli_main
from reckon.crew import findings as findings_module
from reckon.crew import review as review_module

PROJECT = "path-findings"
FILE = "reckon/a.py"
RUN_A = "r-reviewed-a"
RUN_B = "r-reviewed-b"
RUN_C = "r-reviewed-c"
REVIEW_A = "r-20261008T090000000000-review-a"
REVIEW_B = "r-20261008T091000000000-review-b"
REVIEW_C = "r-20261008T092000000000-review-c"


@pytest.fixture()
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised project checkout holding the committed reviews tree."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    return root


def _report_path(checkout: Path) -> str:
    """An absolute report path outside the checkout, as a reviewer cites one."""
    return str(
        checkout.parent / "config" / "crew" / "reports" / PROJECT / "health.html"
    )


def _store(checkout: Path, record: dict) -> Path:
    return review_module.store_committed_review(record, root=checkout)


def _run_record(
    reviewed_run_id: str,
    review_run_id: str,
    findings: list[dict],
) -> dict:
    return {
        "project": PROJECT,
        "reviewed_run_id": reviewed_run_id,
        "review_run_id": review_run_id,
        "reviewed_head_sha": "0" * 40,
        "scores": {"evidence": 17},
        "findings": findings,
    }


def _three_records(checkout: Path) -> None:
    # Two reviews name the same repository path — one blocking, one follow-on.
    _store(
        checkout,
        _run_record(
            RUN_A,
            REVIEW_A,
            [
                {
                    "file": FILE,
                    "line": "12",
                    "text": "unguarded index",
                    "severity": "blocking",
                }
            ],
        ),
    )
    _store(
        checkout,
        _run_record(
            RUN_B,
            REVIEW_B,
            [
                {
                    "file": FILE,
                    "line": "40",
                    "text": "naming would read better",
                    "severity": "follow-on",
                }
            ],
        ),
    )
    # A third names a report path, which is not inside the repository.
    _store(
        checkout,
        _run_record(
            RUN_C,
            REVIEW_C,
            [
                {
                    "file": _report_path(checkout),
                    "line": "7",
                    "text": "report cites a stale figure",
                    "severity": "follow-on",
                }
            ],
        ),
    )


def _group(payload: dict, path: str) -> dict:
    matches = [item for item in payload["paths"] if item["path"] == path]
    assert matches, f"no group for {path!r}: {[i['path'] for i in payload['paths']]}"
    return matches[0]


def test_the_reader_groups_by_path_with_recurrence(checkout: Path) -> None:
    _three_records(checkout)

    payload = findings_module.findings_by_path(PROJECT, path="reckon/", root=checkout)

    group = _group(payload, FILE)
    assert group["scope"] == "repository"
    # Two distinct reviews name the path, so recurrence is 2.
    assert group["recurrence"] == 2
    rows = group["rows"]
    assert len(rows) == 2
    assert {row["severity"] for row in rows} == {"blocking", "follow-on"}
    assert {row["line"] for row in rows} == {"12", "40"}
    assert all(row["disposition"] == "open" for row in rows)


def test_a_report_path_is_an_artefact_and_adds_no_recurrence(checkout: Path) -> None:
    _three_records(checkout)

    payload = findings_module.findings_by_path(PROJECT, root=checkout)

    report = _report_path(checkout)
    assert [row["path"] for row in payload["artefacts"]] == [report]
    artefact = payload["artefacts"][0]
    assert artefact["scope"] == "artefact"
    # The report path is not a repository path, so it forms no group and its
    # single review does not raise any path's recurrence above what the tree
    # itself produced.
    assert report not in [item["path"] for item in payload["paths"]]

    # The prefix filter still admits the repository path and drops the artefact.
    scoped = findings_module.findings_by_path(PROJECT, path="reckon/", root=checkout)
    assert [item["path"] for item in scoped["paths"]] == [FILE]
    assert scoped["artefacts"] == []


def test_the_mcp_view_serves_the_same_rows(checkout: Path) -> None:
    _three_records(checkout)

    payload = mcp._crew(
        PROJECT, view="findings", path="reckon/", checkout_path=str(checkout)
    )

    assert payload["ok"] is True
    assert payload["view"] == "findings"
    group = _group(payload, FILE)
    assert group["recurrence"] == 2
    assert all(row["disposition"] == "open" for row in group["rows"])


def test_the_cli_prints_the_same_rows_as_json(checkout: Path) -> None:
    _three_records(checkout)

    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "ledger",
            "--project",
            PROJECT,
            "--view",
            "findings",
            "--path",
            "reckon/",
            "--checkout-path",
            str(checkout),
        ],
    )
    assert result.exit_code == 0, result.output
    cli_payload = json.loads(result.output)

    view_payload = mcp._crew(
        PROJECT, view="findings", path="reckon/", checkout_path=str(checkout)
    )

    def _key(row: dict) -> tuple:
        return (row["path"], row["line"], row["severity"], row["text"])

    assert sorted(_key(r) for r in cli_payload["rows"]) == sorted(
        _key(r) for r in view_payload["rows"]
    )
    assert cli_payload["paths"] == view_payload["paths"]
