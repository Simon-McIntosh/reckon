"""A tick's queue ordering reads the run store's section history once.

The queue ordering rebuilds the roadmap whenever a plan input moves, and most
ticks carry one: a plan edited, a tick written to the sprint index. Every
rebuild asks :mod:`reckon.crew.section_holds` for the project's promoted rows,
and those rows are the run store's, which gains a few an hour. Reading them per
rebuild decodes the whole project's history out of a SQLite file on a shared
filesystem so that the answer can come out identical.

The population here is the production shape: 2,000 promoted rows over three
plans, and 20 queued requests, in a temporary crew home. The ordering is
computed three times, each after an input the roadmap watches moved so that it
rebuilds the roadmap rather than reusing its own priority build, and the rows
every read of the store decodes are counted by a stand-in. The first ordering
decodes the population; the second and third decode nothing and place the queue
exactly as the first did. Recording one promotion reads the store again, and
the section history the ordering was given carries the promoted row.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon import _store, run_store
from reckon.crew import section_holds
from reckon.crew.admission import ordering
from tests import test_a_queued_request_waits_on_its_dependencies as fixtures

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)
PROJECT = fixtures.PROJECT
PLANS = ("plan-a", "plan-b", "plan-c")
SECTIONS = ("s1", "s2", "s3")
#: The population the store answers for: the row count one read decodes, and the
#: number of queued requests the ordering places beside it.
POPULATION = 2000
QUEUED = 20
#: The section a promotion is recorded against, and the one the store's
#: population leaves free so the recorded promotion is its first evidence.
AFFECTED = ("plan-a", "s1")


def _plan_html(slug: str) -> str:
    """One plan of the fixture's shape: three implementable sections."""
    quote = '"'
    declarations = dict.fromkeys(SECTIONS, "implementable")
    encoded = json.dumps(declarations).replace(quote, "&quot;")
    headings = "".join(
        f"<h2 id={quote}{section}{quote}>section {section[1:]}</h2>"
        for section in SECTIONS
    )
    return (
        "<!doctype html>\n"
        f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>\n"
        f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>\n"
        f"<meta name={quote}plan-slug{quote} content={quote}{slug}{quote}>\n"
        f"<meta name={quote}plan-status{quote} content={quote}active{quote}>\n"
        f"<meta name={quote}plan-section-declarations{quote} "
        f"content={quote}{encoded}{quote}>\n"
        f"{headings}"
    )


@pytest.fixture()
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The fixture repo, plus the two plans the population spans."""
    repository = fixtures.chain_repo.__wrapped__(tmp_path, monkeypatch)
    for slug in PLANS[1:]:
        path = repository / "docs" / "plans" / f"{slug}.html"
        path.write_text(_plan_html(slug), encoding="utf-8")
        fixtures._run(repository, "add", "--", str(path.relative_to(repository)))
    fixtures._run(repository, "commit", "-q", "-m", "chore: seed the other plans")
    return repository


def _record(index: int, *, plan: str, section: str, node: str) -> dict[str, Any]:
    """One promoted row, as promotion writes it."""
    completed = NOW - timedelta(hours=index)
    return {
        "run_id": f"r-2026-01-01T{index:06d}Z-{node}",
        "project": PROJECT,
        "member": f"worker-{index % 7}",
        "node": node,
        "plan": plan,
        "section": section,
        "role": "implement",
        "gate": "passed",
        "dispatched_at": (completed - timedelta(minutes=30)).isoformat(),
        "completed_at": completed.isoformat(),
    }


def _seed_population() -> None:
    """2,000 promoted rows over the three plans, leaving the affected section free."""
    store = run_store.RunStore(run_store.store_path())
    for index in range(POPULATION):
        plan = PLANS[index % len(PLANS)]
        section = SECTIONS[index % len(SECTIONS)]
        if (plan, section) == AFFECTED:
            plan, section = "plan-c", "s1"
        store.append(
            PROJECT,
            _record(index, plan=plan, section=section, node=f"seed-{index:04d}"),
        )


def _pointer(index: int) -> dict[str, Any]:
    """One queued request, on every plan and section the fixture carries."""
    plan = PLANS[index % len(PLANS)]
    section = SECTIONS[(index // len(PLANS)) % len(SECTIONS)]
    return {
        "run_id": f"q-{index:02d}",
        "project": PROJECT,
        "plan": plan,
        "section": section,
        "role": "implement",
        "node": {
            "id": f"q-node-{index:02d}",
            "plan": plan,
            "section": section,
            "role": "implement",
        },
        "session": f"coordinator-{index % 3}",
        "backend": "clive",
        "phase": "queued",
        "queued_at": (NOW - timedelta(seconds=10 + index)).isoformat(),
    }


def _pointers() -> list[dict[str, Any]]:
    """20 queued requests over the queue's sessions, plans and sections."""
    return [_pointer(index) for index in range(QUEUED)]


def _count_decodes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record how many rows every read of the store decodes."""
    decoded: list[int] = []
    owner = run_store.stored_run_rows

    def recording(*args: Any, **kwargs: Any) -> Any:
        rows = owner(*args, **kwargs)
        if rows is not None:
            decoded.append(len(rows))
        return rows

    monkeypatch.setattr(run_store, "stored_run_rows", recording)
    return decoded


def _move_a_plan_input(checkout: Path) -> None:
    """Move one input the roadmap watches so the next ordering rebuilds.

    The ordering reuses a roadmap build while the queued plans' priority state
    and the sprint inputs beside them are unchanged, and a tick carrying a plan
    edit or a sprint write rebuilds it. The sprint index is the input whose
    identity moves while its content does not, so the ordering that follows
    reads the same priorities a reused build would have.
    """
    path = _store.state_path(PROJECT, "index", root=checkout)
    observed = path.stat()
    os.utime(path, ns=(observed.st_atime_ns, observed.st_mtime_ns + 1_000_000))


def _order(checkout: Path) -> dict[str, dict[str, Any]]:
    _move_a_plan_input(checkout)
    return ordering.queued_placements(_pointers(), now=NOW)


def test_an_unchanged_store_answers_every_rebuild_without_a_read(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_population()
    decoded = _count_decodes(monkeypatch)

    first = _order(checkout)
    read = sum(decoded)
    assert read >= POPULATION, (
        "the first ordering must read the population, or the count measures "
        f"nothing: {decoded}"
    )
    del decoded[:]
    second = _order(checkout)
    third = _order(checkout)
    assert decoded == [], f"an unchanged store decodes no row: {decoded}"
    assert second == first
    assert third == first


def test_a_recorded_promotion_is_read_and_holds_its_section(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_population()
    decoded = _count_decodes(monkeypatch)

    before = _order(checkout)
    read = sum(decoded)
    assert read >= POPULATION
    _order(checkout)
    assert sum(decoded) == read, "an unchanged store is not read again"

    delivered = _record(
        9_000, plan=AFFECTED[0], section=AFFECTED[1], node="delivered-now"
    )
    run_store.append(PROJECT, delivered)
    del decoded[:]
    after = _order(checkout)
    assert sum(decoded) >= POPULATION, (
        "a promotion is read again rather than answered from the store's old "
        f"contents: {decoded}"
    )
    # The queue's own places are read from the fixture's live set, which the
    # promotion does not touch, so the ordering itself is the answer the store's
    # previous contents gave. What moves is the section history the ordering was
    # built from, and it is asserted here rather than assumed: the affected
    # section reads held by the row just promoted, which is what a reader
    # holding no cache derives from the same store.
    assert after == before, f"the delivered section moves no queue place: {after}"
    holds = section_holds.held_section_blockers(PROJECT, checkout)
    promoted = [
        row["run_id"]
        for row in holds.get(AFFECTED, [])
        if str(row.get("state")) == "promoted"
    ]
    assert promoted == [delivered["run_id"]], (
        f"the delivered row holds its section: {holds.get(AFFECTED)}"
    )


def test_a_store_that_moves_while_it_is_read_is_not_kept(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A promotion landing mid-read leaves rows one write short of their token.

    The rows read before the write are not cached under the token read after it,
    so the next ordering reads the store rather than answering from a read that
    never saw the promotion.
    """
    _seed_population()
    owner = run_store.stored_run_rows
    written: list[int] = []

    def recording(*args: Any, **kwargs: Any) -> Any:
        rows = owner(*args, **kwargs)
        # The write lands after the population's own read has returned and
        # before the token beside the read is taken, which is the window the
        # read must not be kept across.
        if rows is not None and len(rows) >= POPULATION and not written:
            written.append(1)
            run_store.append(
                PROJECT,
                _record(9_001, plan="plan-c", section="s2", node="landed-during"),
            )
        return rows

    monkeypatch.setattr(run_store, "stored_run_rows", recording)
    _order(checkout)

    assert written, "the population read is the one the write lands on"
    decoded = _count_decodes(monkeypatch)
    _order(checkout)
    assert sum(decoded) >= POPULATION, (
        "the rows read before the write are not kept under the token read "
        f"after it: the next ordering decodes {decoded}"
    )
