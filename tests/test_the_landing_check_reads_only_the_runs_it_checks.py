"""The landing audit decodes only the promoted runs it checks.

A plan's whole promotion history decoded inside one read-only store connection
holds the run store's read lock for as long as the decode runs, and every
concurrent promotion waits on that lock until its write times out with
``database is locked``. The audit looks at the promoted runs of the sections a
plan declares done — a handful on a large plan, none at all on a plan with
nothing declared done — so those rows are the whole read.

The test runs on a temporary crew home and a temporary git repository whose
store holds 2,000 promoted rows for one plan. Three of them sit on a section the
plan declares done, and one of the three records a head the primary branch
cannot reach. A recording stand-in on ``run_store._record_rows`` counts the rows
the plan-scoped full-payload read decodes, because that read is the one that
blocked promotion.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import ledger, roadmap, run_store

PROJECT = "sample"
PLAN = "alpha"
ROWS = 2_000
# The sections that carry the bulk of the history and are not declared done:
# their rows are what a whole-plan decode would have paid for.
BULK_SECTIONS = ("s2", "s1", "s9")
DONE_SECTION = "s5"
DONE_ROWS = 3


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=repository, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _plan_html_text(declarations: dict[str, str]) -> str:
    quote = '"'
    sections = "".join(
        f"<h2 id={quote}{name}{quote}>§{name[1:]}</h2>"
        f"<p>Write paths: <code>{name}.py</code>.</p>"
        f"<p><strong>Done when</strong> the {name} check passes.</p>"
        for name in declarations
    )
    return (
        "<!doctype html>\n"
        f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>\n"
        f"<meta name={quote}index-project{quote} content={quote}{PROJECT}{quote}>\n"
        f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>\n"
        f"<meta name={quote}plan-slug{quote} content={quote}{PLAN}{quote}>\n"
        f"<meta name={quote}plan-title{quote} content={quote}Alpha{quote}>\n"
        f"<meta name={quote}plan-status{quote} content={quote}active{quote}>\n"
        f"<meta name={quote}plan-section-declarations{quote} content={quote}"
        f"{json.dumps(declarations).replace(quote, '&quot;')}{quote}>\n"
        f"{sections}\n"
    )


def _inventory(declarations: dict[str, str]) -> list[dict]:
    return [
        {
            "slug": PLAN,
            "title": "Alpha",
            "type": "plan",
            "status": "active",
            "impl": 0.0,
            "depends_on": [],
            "effort": "M",
            "roi": "high",
            "blocking": [],
            "gates": [],
            "followups": [],
            "section_declarations": dict(declarations),
            "sections": [],
        }
    ]


def _promoted_row(*, run_id: str, section: str, node: str, head: str) -> dict:
    return ledger.build_record(
        run_id=run_id,
        plan=PLAN,
        section=section,
        node=node,
        role="implement",
        gate="passed",
        commits=[head],
        completed_at="2026-11-10T10:00:00Z",
    )


@pytest.fixture()
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home and a temporary git repository holding the store."""

    crew_home = tmp_path / "crew-home"
    crew_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(crew_home))
    monkeypatch.setenv("RECKON_RUN_STORE", str(crew_home / "crew" / "run_store.db"))

    repository = tmp_path / "checkout"
    repository.mkdir()
    docs = repository / "docs"
    (docs / "plans").mkdir(parents=True)
    (docs / "state" / PROJECT).mkdir(parents=True)
    (docs / "state" / PROJECT / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    (crew_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(docs)}), encoding="utf-8"
    )

    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repository, "add", "--", "seed.txt")
    _git(repository, "commit", "-q", "-m", "chore: seed")
    return repository


def _heads(repository: Path) -> tuple[str, str]:
    """The landed head and a side head the primary branch cannot reach."""

    landed = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "-q", "-b", "side")
    (repository / "side.txt").write_text("side\n", encoding="utf-8")
    _git(repository, "add", "--", "side.txt")
    _git(repository, "commit", "-q", "-m", "feat: side work")
    unlanded = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "-q", "main")
    return landed, unlanded


def _fill_store(repository: Path) -> str:
    """Write the plan's 2,000 promoted rows, returning the unlanded run's id."""

    landed, unlanded = _heads(repository)
    unlanded_run = ""
    with run_store.RunStore() as store:
        for index in range(ROWS - DONE_ROWS):
            section = BULK_SECTIONS[index % len(BULK_SECTIONS)]
            store.append(
                PROJECT,
                _promoted_row(
                    run_id=f"run-bulk-{index:04d}",
                    section=section,
                    node=f"n-bulk-{index:04d}",
                    head=landed,
                ),
            )
        for index in range(DONE_ROWS):
            run_id = f"run-done-{index}"
            head = unlanded if index == DONE_ROWS - 1 else landed
            if head == unlanded:
                unlanded_run = run_id
            store.append(
                PROJECT,
                _promoted_row(
                    run_id=run_id,
                    section=DONE_SECTION,
                    node=f"n-done-{index}",
                    head=head,
                ),
            )
    return unlanded_run


@pytest.fixture()
def decoded(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Every plan-scoped full-payload row read, per call."""

    calls: list[dict] = []
    original = run_store._record_rows

    def recording(connection, **kwargs):
        rows = 0
        for row in original(connection, **kwargs):
            rows += 1
            yield row
        calls.append(
            {
                "plan": kwargs.get("plan"),
                "fields": kwargs.get("fields"),
                "run_ids": kwargs.get("run_ids"),
                "rows": rows,
            }
        )

    monkeypatch.setattr(run_store, "_record_rows", recording)
    return calls


def _landing_rows(calls: list[dict]) -> int:
    """The rows the landing audit's own full-payload, plan-scoped read decoded."""

    return sum(
        call["rows"]
        for call in calls
        if call["plan"] == PLAN and call["fields"] is None
    )


def _landing_calls(calls: list[dict]) -> list[dict]:
    return [call for call in calls if call["plan"] == PLAN and call["fields"] is None]


def test_the_landing_check_decodes_only_the_runs_it_checks(
    checkout: Path, decoded: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    unlanded_run = _fill_store(checkout)
    declarations = {DONE_SECTION: "done"} | dict.fromkeys(
        BULK_SECTIONS, "implementable"
    )
    (checkout / "docs" / "plans" / f"{PLAN}.html").write_text(
        _plan_html_text(declarations), encoding="utf-8"
    )
    monkeypatch.setattr("reckon.crew.list_live", list)

    report = roadmap.build_roadmap(
        PROJECT, _inventory(declarations), [], docs_dir=checkout / "docs"
    )

    # The instrument sees the population the store holds, so an absent landing
    # read cannot be mistaken for a cheap one.
    assert len(run_store.stored_run_rows(PROJECT, fields=("run_id",)) or []) == ROWS
    calls = _landing_calls(decoded)
    assert len(calls) == 1, calls
    assert calls[0]["run_ids"], "the landing read must name the runs it checks"
    assert _landing_rows(decoded) <= DONE_ROWS

    found = [
        finding
        for finding in report["wiring_findings"]
        if finding.get("code") == "run-not-landed"
    ]
    assert len(found) == 1
    assert found[0]["extra"]["run_id"] == unlanded_run
    assert found[0]["extra"]["section"] == DONE_SECTION


def test_a_plan_with_no_done_section_decodes_no_rows(
    checkout: Path, decoded: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    _fill_store(checkout)
    declarations = dict.fromkeys(BULK_SECTIONS, "implementable")
    (checkout / "docs" / "plans" / f"{PLAN}.html").write_text(
        _plan_html_text(declarations), encoding="utf-8"
    )
    monkeypatch.setattr("reckon.crew.list_live", list)

    report = roadmap.build_roadmap(
        PROJECT, _inventory(declarations), [], docs_dir=checkout / "docs"
    )

    assert _landing_rows(decoded) == 0
    assert [
        finding
        for finding in report["wiring_findings"]
        if finding.get("code") == "run-not-landed"
    ] == []
