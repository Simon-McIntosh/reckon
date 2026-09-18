"""The portfolio is composed from fake mounted projects, not from this host.

Every source the composition reads is injected, including the liveness
classifier, so the assertions describe the composition rather than the
workstation the suite happens to run on: a fixture that named a real mount, or
a real live pointer, would report on the machine and pass wherever the machine
happened to agree.
"""

from __future__ import annotations

from pathlib import Path

from reckon.flight import PORTFOLIO_COLUMNS, flight_report, portfolio_report
from reckon.mcp_views import portfolio_view


def _classifier(pointer: dict) -> dict:
    """Stand in for the process-table classifier, reading the fixture's answer.

    The real classifier derives exactly these two facts — the classification
    and whether the process is alive — from the pointer and the process table.
    The fixture states them instead, which keeps the composition under test
    independent of whose processes are running when the suite executes.
    """
    return {
        "run_id": pointer.get("run_id"),
        "plan": pointer.get("plan"),
        "classification": pointer.get("classification", "running"),
        "process_alive": pointer.get("process_alive"),
    }


def _pointer(project: str, run_id: str, plan: str, **overrides: object) -> dict:
    pointer = {
        "project": project,
        "run_id": run_id,
        "plan": plan,
        "role": "implement",
        "classification": "running",
        "process_alive": True,
        "closure_disposition": None,
    }
    pointer.update(overrides)
    return pointer


def _roadmaps() -> dict[str, dict]:
    """Two projects whose uncovered hours and names rank in opposite orders.

    The project with the hours still to cover sorts last alphabetically, so a
    table sorted by name rather than by uncovered critical hours fails here
    instead of passing by coincidence.
    """
    return {
        "alpha": {
            "active_sprint_id": None,
            "sprints": [],
            "critical_path": {
                "plans": ["a-one"],
                "length_hours": 8.0,
                "length_unit": "elapsed-hours",
                "worker_hours": 8.0,
                "effort_unit": "worker-hours",
            },
        },
        "zeta": {
            "active_sprint_id": "S7",
            "sprints": [{"id": "S7", "theme": "Zeta theme"}],
            "critical_path": {
                "plans": ["z-one", "z-two"],
                "length_hours": 12.0,
                "length_unit": "elapsed-hours",
                "worker_hours": 30.0,
                "effort_unit": "worker-hours",
            },
        },
    }


def _sources(tmp_path: Path) -> dict:
    roadmaps = _roadmaps()
    lanes = {
        "alpha": {"lane_headroom": "unknown", "lane_reading_age_seconds": None},
        "zeta": {"lane_headroom": 40, "lane_reading_age_seconds": 25},
    }
    pointers = [
        # A live run standing on alpha's only path plan leaves it fully covered.
        _pointer("alpha", "r-a-live", "a-one"),
        # A run whose process has stopped covers nothing and is unreconciled.
        _pointer(
            "alpha",
            "r-a-dead",
            "a-one",
            process_alive=False,
            classification="abandoned",
            role="review",
        ),
        # A run that is off the critical path widens the project without
        # covering it, so live width and coverage stay separate figures.
        _pointer("alpha", "r-a-role", "a-off-path", role="investigate"),
        # A live run covers one of zeta's two path plans, leaving the other bare.
        _pointer("zeta", "r-z-live", "z-one"),
        # A handed-off pointer is reconciled, and it still counts toward the
        # width while it stands off the critical path.
        _pointer(
            "zeta",
            "r-z-handoff",
            "z-off-path",
            role="review",
            closure_disposition={"kind": "handed-off"},
        ),
        # A disposition outlived by its run excuses nothing: the run has stopped
        # working, so the pointer is unreconciled again.
        _pointer(
            "zeta",
            "r-z-stale",
            "z-off-path",
            classification="complete",
            closure_disposition={"kind": "still-working"},
        ),
    ]
    return {
        "mounts": {
            "alpha": tmp_path / "Alpha" / "docs",
            "zeta": tmp_path / "Zeta" / "docs",
        },
        "live_pointers": pointers,
        "roadmap_reader": lambda project, _docs: roadmaps[project],
        "lane_reader": lambda project, _docs: lanes[project],
        "classifier": _classifier,
    }


def _rows_by_project(report: dict) -> dict[str, dict]:
    return {row["project"]: row for row in report["rows"]}


def test_rows_sort_by_uncovered_critical_hours_descending(tmp_path: Path) -> None:
    report = portfolio_report(**_sources(tmp_path))

    assert [row["project"] for row in report["rows"]] == ["zeta", "alpha"]


def test_row_carries_every_column(tmp_path: Path) -> None:
    report = portfolio_report(**_sources(tmp_path))
    rows = _rows_by_project(report)

    assert report["columns"] == list(PORTFOLIO_COLUMNS)
    for row in report["rows"]:
        assert set(PORTFOLIO_COLUMNS) <= set(row)

    zeta = rows["zeta"]
    assert zeta["pushed_sprint"] == {"id": "S7", "theme": "Zeta theme"}
    assert zeta["critical_path"]["plans"] == ["z-one", "z-two"]
    assert zeta["coverage"] == {
        "plans": 2,
        "covered_plans": 1,
        "covered_fraction": 0.5,
        "live_runs": 1,
        "runs": [{"run_id": "r-z-live", "plan": "z-one"}],
    }
    assert zeta["uncovered_critical_hours"] == 6.0
    assert zeta["live_width"] == 3
    assert zeta["live_width_by_role"] == {"implement": 2, "review": 1}
    assert zeta["unreconciled_runs"] == 1
    assert zeta["lane_headroom"] == 40
    assert zeta["lane_reading_age_seconds"] == 25

    alpha = rows["alpha"]
    assert alpha["pushed_sprint"] == {"id": None, "theme": ""}
    assert alpha["coverage"] == {
        "plans": 1,
        "covered_plans": 1,
        "covered_fraction": 1.0,
        "live_runs": 1,
        "runs": [{"run_id": "r-a-live", "plan": "a-one"}],
    }
    assert alpha["uncovered_critical_hours"] == 0.0
    assert alpha["lane_headroom"] == "unknown"
    assert alpha["lane_reading_age_seconds"] is None


def test_a_stopped_process_covers_nothing_and_reads_unreconciled(
    tmp_path: Path,
) -> None:
    report = portfolio_report(**_sources(tmp_path))
    rows = _rows_by_project(report)

    # alpha carries one live and one stopped pointer on the same plan. Only the
    # living one is a live width, and only the stopped one is unreconciled.
    assert rows["alpha"]["live_width"] == 2
    assert rows["alpha"]["unreconciled_runs"] == 1
    assert rows["alpha"]["coverage"]["plans"] == 1


def test_totals_are_the_fleet_figures(tmp_path: Path) -> None:
    report = portfolio_report(**_sources(tmp_path))

    assert report["projects"] == 2
    assert report["live_width"] == 5
    assert report["unreconciled_runs"] == 2
    assert report["uncovered_critical_hours"] == 6.0
    assert report["errors"] == []


def test_a_failing_project_reports_its_refusal_and_keeps_the_table(
    tmp_path: Path,
) -> None:
    sources = _sources(tmp_path)
    roadmaps = _roadmaps()

    def reader(project: str, docs: Path) -> dict:
        if project == "zeta":
            raise RuntimeError("no roadmap")
        return roadmaps[project]

    sources["roadmap_reader"] = reader
    report = portfolio_report(**sources)
    rows = _rows_by_project(report)

    assert report["errors"] == ["zeta: RuntimeError: no roadmap"]
    assert rows["zeta"]["error"] == "zeta: RuntimeError: no roadmap"
    assert rows["zeta"]["uncovered_critical_hours"] is None
    # The readable project still answers, and an unmeasured row sorts last
    # rather than posing as a project with nothing left to cover.
    assert [row["project"] for row in report["rows"]] == ["alpha", "zeta"]


def test_star_project_key_answers_with_the_portfolio(tmp_path: Path) -> None:
    report = flight_report("*", portfolio_sources=_sources(tmp_path))

    assert report["project"] == "*"
    assert [row["project"] for row in report["portfolio"]["rows"]] == ["zeta", "alpha"]

    view = portfolio_view(report["portfolio"])
    assert view["project"] == "*"
    assert view["view"] == "summary"
    assert view["totals"] == {
        "projects": 2,
        "live_width": 5,
        "unreconciled_runs": 2,
        "uncovered_critical_hours": 6.0,
    }
    assert "runs" not in view["rows"][0]["coverage"]
    assert view["rows"][0]["coverage"]["live_runs"] == 1

    detail = portfolio_view(report["portfolio"], view="detail")
    assert detail["rows"][0]["coverage"]["runs"] == [
        {"run_id": "r-z-live", "plan": "z-one"}
    ]

    raw = portfolio_view(report["portfolio"], view="raw")
    assert raw["data"]["projects"] == 2
    assert len(raw["data"]["rows"]) == 2
