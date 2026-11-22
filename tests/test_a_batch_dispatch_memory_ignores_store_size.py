"""A batch dispatch decodes rows for the nodes it names, not for the store.

A ``crew dispatch --batch`` reads the run store to answer whether each node it
is asked to queue has already been delivered. The answer for one node lives in
that node's own promoted rows, but the read that produced it walked the
project's whole history: the section-hold read fell through to the committed
ledger's picker projection when its node filter matched nothing, the roadmap's
section-history read decoded every plan's promoted rows, and its attempts
aggregate decoded the whole picker projection again. Each of those makes the
batch's peak resident memory grow with the store's delivered-run count instead
of with the plans the batch names, and a queueing coordinator was measured
holding gigabytes to place eight entries.

The cases below count the rows the store decodes — through its one decode
point — for one batch over one store and for the same batch over that store
with its delivered-run rows doubled. The count is the same, and it is the
named node's own rows.

The first case fails against the read this change replaced: that read decoded
the whole population, so doubling the population doubled the count.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon import run_store
from reckon.crew import dispatch_batch, section_holds

PROJECT = "proj"
SECTION = "s1"
#: The plan whose node the batch queues: nothing has been delivered for it.
FREE_PLAN = "plan-free"
#: The plan whose node a run has already delivered: the batch reads that row.
DELIVERED_PLAN = "plan-delivered"
#: Plans carried only so the project looks like a real one; the delivered rows
#: below are spread across them.
OTHER_PLANS = ("plan-other-a", "plan-other-b")

#: Promoted rows in the small store. Doubled for the second arm, so a read that
#: decodes the population answers twice as many rows there.
DELIVERED = 60

NOW = "2026-11-22T00:00:00Z"


def _config() -> dict:
    return {
        "default_backend": "clive",
        "local_backend": "clive",
        "backends": {
            "clive": {
                "launch": "cli",
                "command": "codex",
                "model": "local-model",
                "effort": "medium",
                "sandbox": "worktree-full",
                "time_budget": "25m",
            }
        },
        "roles": {"implement": {}},
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }


def _plan(slug: str) -> str:
    quote = '"'
    declarations = json.dumps({SECTION: "implementable"})
    body = (
        f"<h2 id={quote}{SECTION}{quote}>§1</h2>"
        f"<p>Build {slug} {SECTION}.</p>"
        f"<p><strong>Done when</strong> the {slug} check passes.</p>"
    )
    return (
        "<!doctype html>\n"
        f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>\n"
        f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>\n"
        f"<meta name={quote}plan-slug{quote} content={quote}{slug}{quote}>\n"
        f"<meta name={quote}plan-status{quote} content={quote}active{quote}>\n"
        f"<meta name={quote}plan-section-declarations{quote} content={quote}"
        f"{declarations.replace(quote, '&quot;')}{quote}>\n"
        f"{body}\n"
    )


def _entries(*slugs: str) -> list[dict]:
    """One entry per named plan, queueing that plan's only section node."""

    return [
        {
            "project": PROJECT,
            "plan": slug,
            "section": SECTION,
            "node": f"{slug}-{SECTION}",
            "goal": f"build {slug} {SECTION}",
            "time_budget": "20m",
            "spec_level": "exact",
        }
        for slug in slugs
    ]


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _checkout(tmp_path: Path) -> Path:
    main = tmp_path / "checkout"
    state = main / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    plans_dir = main / "docs" / "plans"
    for slug in (FREE_PLAN, DELIVERED_PLAN, *OTHER_PLANS):
        plans_dir.mkdir(parents=True, exist_ok=True)
        (plans_dir / f"{slug}.html").write_text(_plan(slug), encoding="utf-8")
    _git(main, "init", "-q", "-b", "main")
    _git(main, "config", "user.email", "worker@example.invalid")
    _git(main, "config", "user.name", "Worker")
    _git(main, "add", "--", "docs")
    _git(main, "commit", "-q", "-m", "chore: seed checkout")
    return main


def _record(run_id: str, *, node: str, plan: str) -> dict[str, Any]:
    """One promoted row carrying the wide detail a real promotion records."""

    return {
        "run_id": run_id,
        "project": PROJECT,
        "node": node,
        "plan": plan,
        "section": SECTION,
        "role": "implement",
        "gate": "passed",
        "completed_at": NOW,
        # A promotion's wide half: kilobytes per row that the read must not
        # bring into the process to answer which node, if any, was delivered.
        "node_definition": {"id": node, "goal": "g" * 4000},
        "budget": {"sent_tokens": 123456, "notes": "b" * 4000},
    }


def _store_with_deliveries(
    path: Path,
    delivered: int,
    *,
    tag: str = "",
    delivered_row: bool = True,
) -> Path:
    """A store holding the delivered node's row and ``delivered`` other rows."""

    store = run_store.RunStore(path)
    if delivered_row:
        store.append(
            PROJECT,
            _record(
                f"r-delivered{tag}",
                node=f"{DELIVERED_PLAN}-{SECTION}",
                plan=DELIVERED_PLAN,
            ),
        )
    for index in range(delivered):
        plan = OTHER_PLANS[index % len(OTHER_PLANS)]
        store.append(
            PROJECT,
            _record(f"r-other-{index:04d}{tag}", node=f"other-{index:04d}", plan=plan),
        )
    store.close()
    return path


def _decoded_rows(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Count the rows the store decodes, through its one decode point."""

    decoded: list[str] = []
    owner = run_store._decode_run_row

    def recording(payload: str, detail: str | None) -> dict[str, Any]:
        decoded.append(str(len(payload)))
        return owner(payload, detail)

    monkeypatch.setattr(run_store, "_decode_run_row", recording)
    return decoded


def _reset_caches() -> None:
    section_holds._HISTORY_READS.clear()
    run_store._HELD_ANSWERS.clear()


def _run_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str,
    checkout: Path,
    store: Path,
    slugs: tuple[str, ...],
) -> tuple[list[str], dict]:
    """Queue the batch from its own home, reading the store named here."""

    home = tmp_path / name
    home.mkdir()
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(checkout / "docs")}),
        encoding="utf-8",
    )
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_RUN_STORE", str(store))
    _reset_caches()
    decoded = _decoded_rows(monkeypatch)

    result = dispatch_batch.queue_batch(
        _entries(*slugs),
        session=name,
        config_for=lambda _project: copy.deepcopy(_config()),
        repo_root=checkout,
    )
    return decoded, result


def test_doubling_the_store_does_not_change_what_the_batch_decodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The batch's read is bounded by the nodes it names, not by store size."""

    checkout = _checkout(tmp_path)
    small = _store_with_deliveries(tmp_path / "small.db", DELIVERED)
    big = tmp_path / "doubled.db"
    shutil.copyfile(small, big)
    _store_with_deliveries(big, DELIVERED, tag="x", delivered_row=False)

    one_population, first = _run_batch(
        tmp_path,
        monkeypatch,
        name="arm-one",
        checkout=checkout,
        store=small,
        slugs=(FREE_PLAN,),
    )
    doubled_population, second = _run_batch(
        tmp_path,
        monkeypatch,
        name="arm-two",
        checkout=checkout,
        store=big,
        slugs=(FREE_PLAN,),
    )

    assert len(doubled_population) == len(one_population), (
        "doubling the store's delivered-run rows changed what the batch "
        f"decodes: {len(doubled_population)} rows against {len(one_population)}"
    )
    assert len(one_population) < DELIVERED, (
        "the batch's decode count for the plan it named is not bounded by that "
        f"plan: {len(one_population)} rows against {DELIVERED} for other plans"
    )
    # The free node queues, whichever store the batch read.
    for result in (first, second):
        assert result["refused"] == 0, result["results"]
        assert result["results"][0]["status"] == "queued", result["results"]

    # The control: a batch naming the delivered node reads its row, so the
    # counter is live rather than dead and the equality above is a measurement.
    control, held_result = _run_batch(
        tmp_path,
        monkeypatch,
        name="arm-control",
        checkout=checkout,
        store=big,
        slugs=(DELIVERED_PLAN,),
    )
    assert held_result["results"][0]["status"] == "held", held_result["results"]
    assert len(control) == 1, (
        "the delivered node's own row is the one row read for it: "
        f"{len(control)} rows in a {2 * DELIVERED + 2}-row store"
    )


def test_the_history_index_keeps_a_bounded_number_of_answers() -> None:
    """A process that asks about many plan sets holds the last few, not all.

    The index is keyed by the plans it was asked about, and the plan set comes
    from the caller: a coordinator that dispatches for several projects, or
    queues batches naming different plans, would otherwise keep one entry per
    question for the life of the process, each holding the delivered nodes of
    its plans. Dropping an answer costs a store read and never an answer, so the
    map is capped; this holds the cap and the eviction order.

    The cap is read from the module rather than written here so the case does
    not restate a constant the code owns.
    """

    section_holds._HISTORY_READS.clear()
    cap = section_holds._HISTORY_READ_ENTRIES
    stamp = (1, 2)
    questions = [
        (PROJECT, "/store.db", None, None, (f"plan-{index}",))
        for index in range(cap * 3)
    ]
    for index, key in enumerate(questions):
        section_holds._remember_history_index(
            key, stamp, {(f"plan-{index}", SECTION): {}}
        )

    assert len(section_holds._HISTORY_READS) == cap
    assert questions[-1] in section_holds._HISTORY_READS
    assert questions[0] not in section_holds._HISTORY_READS
    # Re-asking an old question moves it to the newest position rather than
    # adding a second entry for it.
    section_holds._remember_history_index(questions[-1], stamp, {})
    assert len(section_holds._HISTORY_READS) == cap
    section_holds._HISTORY_READS.clear()


def test_a_node_with_no_delivery_costs_no_row_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A node the store holds nothing for is answered without decoding a row.

    The store answers whether it holds the project at all from one indexed
    membership query, so a node with no delivered row costs no decode. The read
    this change replaced took that empty answer as evidence the store held
    nothing and fell through to the committed ledger's picker projection, which
    decoded every delivered row in the project to filter them back down to the
    same empty answer.
    """

    checkout = _checkout(tmp_path)
    store = _store_with_deliveries(tmp_path / "store.db", DELIVERED)

    decoded, result = _run_batch(
        tmp_path,
        monkeypatch,
        name="arm-free",
        checkout=checkout,
        store=store,
        slugs=(FREE_PLAN,),
    )

    assert result["refused"] == 0, result["results"]
    assert result["results"][0]["status"] == "queued", result["results"]
    assert len(decoded) == 0, (
        "a node with no delivered row decoded rows: "
        f"{len(decoded)} rows in a {DELIVERED + 1}-row store"
    )
