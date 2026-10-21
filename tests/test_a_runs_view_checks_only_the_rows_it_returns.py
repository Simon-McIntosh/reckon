"""A runs view reads each project ledger once and probes paths only for its rows.

The runs view compacts every row of a read before it filters, sorts and applies
``limit``. Two costs followed from that. A live row whose pointer and stream
carry no session reached the ledger source with no row in hand, so it loaded the
whole ledger to find the row the view had already read — once for each such row.
And each row's worktree, manifest and transcript were probed for every row
evaluated, though a limited view returns a handful.

This checks both halves against the same fixture and the same base behaviour:
one runs view loads the ledger at most once per project, it probes paths at most
three times per row it returns, and the rows it returns are the rows the eager
projection produced.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import query
from reckon.crew.recovery import classify_pointer, group_terminal_lane_events
from reckon.crew.runs import list_live

PROJECT = "proj"
LEDGER_ROWS = 52
LIVE_RUNS = 3
LIMIT = 5

#: A returned row probes at most three paths: its worktree, its manifest and its
#: transcript. A live row's manifest presence comes from its classification, so
#: it probes two.
PATH_PROBES_PER_ROW = 3


@pytest.fixture()
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    ledger.write(PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=root)
    return root


def _populate(repository: Path) -> list[str]:
    """Promote a project's rows; every one records the session it ran in."""
    run_ids: list[str] = []
    for index in range(LEDGER_ROWS):
        run_id = f"r-20261021T10{index:04d}000000-node-{index:02d}"
        ledger.append_run(
            PROJECT,
            ledger.build_record(
                run_id=run_id,
                plan="plan-a",
                section="§2",
                node=f"node-{index:02d}",
                gate="passed",
                member_id="member-b",
                session_id=f"session-{index:03d}",
                base_sha="def456",
                commits=["fedcba"],
            ),
            root=repository,
        )
        run_ids.append(run_id)
    return run_ids


def _write_live(home: Path, run_id: str, *, node: str, worktree: Path) -> None:
    """Write one live pointer carrying no session of its own.

    ``launch`` is left unset, so the stream source is not consulted and the only
    source that can answer this row is its committed ledger row.

    ``process_alive`` is true, so the row classifies as running. A pointer whose
    process has stopped classifies as interrupted or abandoned, and
    ``classify_pointer`` answers those by resolving the session from the ledger
    itself — one load per such row, inside the classification this section
    leaves alone. This fixture measures the view's own read, so it does not
    stand a classification-time load on top of it.
    """
    worktree.mkdir(parents=True, exist_ok=True)
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "node": {"id": node, "plan": "plan-a", "section": "§2"},
            "phase": "working",
            "process_alive": True,
            "member": "member-a",
            "agent": {"backend": "alpha"},
            "worktree": str(worktree),
            "manifest_path": str(home / "manifests" / f"{run_id}.md"),
            "log_path": str(home / "logs" / f"{run_id}.jsonl"),
        },
    )


def _live_records() -> list[dict]:
    return [
        record for record in list_live() if str(record.get("project") or "") == PROJECT
    ]


def _eager_row(
    record: dict,
    *,
    source: str,
    repository: Path,
    classified: dict | None = None,
) -> dict:
    """One row as the eager projection produced it.

    A live record is handed no ``ledger_row``, which is how the base view asked
    for a live row's session: the ledger source loaded the ledger and searched
    it. The path-derived fields are computed here rather than deferred, which is
    the other half of the behaviour this change replaces.
    """
    return query._compact_row(
        record,
        source=source,
        project=PROJECT,
        checkout_path=str(repository),
        repository=(
            repository if source == "ledger" else query._record_repository(record, {})
        ),
        selected_fields=query.DEFAULT_RUN_FIELDS,
        classified_live=classified,
        ledger_row=record if source == "ledger" else None,
        with_path_fields=True,
    )


def _eager_rows(repository: Path, *, source: str) -> list[dict]:
    """The base view's rows over this fixture, in the view's own order."""
    rows: list[dict] = []
    if source in {"all", "live"}:
        records = _live_records()
        classified = group_terminal_lane_events(
            [classify_pointer(record) for record in records]
        )
        by_id = {str(record.get("run_id") or ""): record for record in records}
        rows.extend(
            _eager_row(
                by_id[str(entry.get("run_id") or "")],
                source="live",
                repository=repository,
                classified=entry,
            )
            for entry in classified
        )
    if source in {"all", "ledger"}:
        rows.extend(
            _eager_row(record, source="ledger", repository=repository)
            for record in ledger.runs(PROJECT, str(repository))
        )
    rows = [
        row
        for row in rows
        if query._matches(
            row,
            node=None,
            plan=None,
            section=None,
            session=None,
            member=None,
            classification=None,
            resumable=None,
            run_id=None,
        )
    ]
    rows.sort(key=lambda row: str(row.get("run_id") or ""), reverse=True)
    return rows


def _write_live_fixture(home: Path, run_ids: list[str]) -> None:
    for index, run_id in enumerate(run_ids[-LIVE_RUNS:]):
        _write_live(
            home,
            run_id,
            node=f"live-{index}",
            worktree=home / "worktrees" / run_id,
        )


def test_a_runs_view_checks_only_the_rows_it_returns(
    isolated_reckon_home: Path,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_ids = _populate(repository)
    _write_live_fixture(isolated_reckon_home, run_ids)
    live_records = _live_records()
    assert len(live_records) == LIVE_RUNS
    assert all("session_id" not in record for record in live_records)
    assert all(
        str(record.get("run_id") or "") in set(run_ids) for record in live_records
    )

    expected = _eager_rows(repository, source="all")

    loads = {"count": 0}
    real_load = ledger.load

    def counting_load(*args: object, **kwargs: object):
        loads["count"] += 1
        return real_load(*args, **kwargs)

    probes = {"count": 0}
    real_path_exists = query._path_exists

    def counting_path_exists(*args: object, **kwargs: object) -> bool:
        probes["count"] += 1
        return real_path_exists(*args, **kwargs)

    monkeypatch.setattr(ledger, "load", counting_load)
    probes["count"] = 0
    monkeypatch.setattr(query, "_path_exists", counting_path_exists)
    answer = query.runs_view(PROJECT, checkout_path=str(repository), limit=LIMIT)

    assert answer["rows"] == expected[:LIMIT], (
        "the limited view no longer returns the rows the eager projection did"
    )
    assert loads["count"] <= 1, (
        f"one runs view made {loads['count']} ledger.load calls over "
        f"{len(run_ids)} committed rows and {LIVE_RUNS} live rows"
    )
    assert probes["count"] <= PATH_PROBES_PER_ROW * len(answer["rows"]), (
        f"{probes['count']} path probes over {len(answer['rows'])} returned rows"
    )

    monkeypatch.setattr(query, "_path_exists", real_path_exists)
    monkeypatch.setattr(ledger, "load", real_load)


def test_a_resumable_filter_returns_the_rows_base_returned(
    isolated_reckon_home: Path,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_ids = _populate(repository)
    _write_live_fixture(isolated_reckon_home, run_ids)
    expected = _eager_rows(repository, source="all")

    # Every row of this fixture is judged: a live pointer whose process has
    # stopped is the one shape that is resumable, and it is left out of the
    # fixture for the reason given on ``_write_live``. Both answers the filter
    # can give are still pinned to base, and the false arm carries every row, so
    # a filter that stopped being applied cannot pass by returning nothing.
    for wanted in (True, False):
        answer = query.runs_view(
            PROJECT, checkout_path=str(repository), resumable=wanted
        )
        assert answer["rows"] == [
            row for row in expected if row["resumable"] is wanted
        ], f"resumable={wanted} no longer returns the rows base returned"
    assert query.runs_view(PROJECT, checkout_path=str(repository), resumable=False)[
        "count"
    ] == len(expected)

    # A caller judging on ``resumable`` is the one caller that pays path probes
    # for rows it does not return, because that is the fact it asked to be
    # judged on. Deferring the fields must not turn that into a silent no-op:
    # the probe count grows with the rows judged rather than with the rows
    # returned.
    probes = {"count": 0}
    real_path_exists = query._path_exists

    def counting_path_exists(*args: object, **kwargs: object) -> bool:
        probes["count"] = probes["count"] + 1
        return real_path_exists(*args, **kwargs)

    monkeypatch.setattr(query, "_path_exists", counting_path_exists)
    probes["count"] = 0
    limited = query.runs_view(
        PROJECT, checkout_path=str(repository), resumable=True, limit=LIMIT
    )
    assert probes["count"] >= len(expected), (
        f"{probes['count']} path probes over {len(expected)} judged rows; a "
        "resumable filter must judge every row the other filters left"
    )
    assert limited["count"] == 0


def test_a_live_view_loads_the_ledger_once_and_keeps_every_row(
    isolated_reckon_home: Path,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_ids = _populate(repository)
    _write_live_fixture(isolated_reckon_home, run_ids)

    expected = _eager_rows(repository, source="live")
    assert len(expected) == LIVE_RUNS
    assert {row["session_id_source"] for row in expected} == {"ledger"}

    loads = {"count": 0}
    real_load = ledger.load

    def counting_load(*args: object, **kwargs: object):
        loads["count"] = loads["count"] + 1
        return real_load(*args, **kwargs)

    monkeypatch.setattr(ledger, "load", counting_load)
    answer = query.runs_view(PROJECT, checkout_path=str(repository), source="live")

    assert answer["rows"] == expected
    assert loads["count"] <= 1, (
        f"one live runs view made {loads['count']} ledger.load calls over "
        f"{LIVE_RUNS} live rows that need their committed row"
    )
