"""An admission pass names the reads inside its release and wait stages.

One pass took 81.6 s and 79.9 s of it went to releasing upstream waits and
resolving holds before a single launch decision, and the receipt named no read.
The pass's receipt now carries, inside each of those two stages, every read it
made with how many times it was made and how long it took, so an 80 s pass
reports the reads that took the 80 s rather than only the total.

The pass here runs over a temporary crew home of 240 live pointers and 80
requests, and the check is the one the receipt is read by: each stage's
per-read times sum to that stage's own time within 5 %, and a receipt with one
read omitted fails that sum.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import queue_admission, runs
from tests import test_each_admission_pass_records_its_verdict as fixtures

PROJECT = "proj"

#: The margin a stage's named reads must account for.
SUMS_WITHIN = 0.05

#: Plans the 80 requests are spread over, so the pass reads more than one.
PLANS = 80
#: Requests stored on each plan.
REQUESTS_PER_PLAN = 1
#: Every plan whose request waits on the pending upstream section; the rest are queued, so the pass
#: has a launch to decide and writes its receipt from it.
PLANS_WAITING = 80
#: The whole live population the pass is measured at.
LIVE_POINTERS = 240


def _plan(slug: str, waiting: bool = True) -> str:
    """One plan document whose first section always waits on a pending upstream.

    The body carries the paragraphs a real plan does, because a read of a plan
    is what these stages spend their time on and a one-line document would make
    the pass look like glue next to reads it never pays for.
    """
    quote = '"'
    declarations = json.dumps(
        {"s1": "implementable", "s2": "implementable"}, separators=(",", ":")
    ).replace('"', "&quot;")
    depends_on = (
        json.dumps({"s1": ["upstream#s2"]}, separators=(",", ":")).replace(
            '"', "&quot;"
        )
        if waiting
        else "{}"
    )
    paragraphs = "\n".join(
        f"<p>Section one of {slug} reads the upstream work {index} times, and the "
        f"pass that resolves its wait reads this plan to learn it.</p>"
        for index in range(700)
    )
    return "\n".join(
        [
            "<!doctype html>",
            f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>",
            f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>",
            f"<meta name={quote}plan-slug{quote} content={quote}{slug}{quote}>",
            f"<meta name={quote}plan-status{quote} content={quote}active{quote}>",
            (
                f"<meta name={quote}plan-section-declarations{quote} "
                f"content={quote}{declarations}{quote}>"
            ),
            (
                f"<meta name={quote}plan-section-depends-on{quote} "
                f"content={quote}{depends_on}{quote}>"
            ),
            f"<h2 id={quote}s1{quote}>Work</h2>",
            paragraphs,
            f"<h2 id={quote}s2{quote}>Upstream</h2>",
        ]
    )


@pytest.fixture()
def populated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The shared admission checkout, holding this node's whole population."""
    repository = fixtures.admit_repo.__wrapped__(tmp_path, monkeypatch)
    plans = repository / "docs" / "plans"
    (plans / "plan-a.html").unlink()
    for index in range(PLANS):
        (plans / f"plan-{index}.html").write_text(
            _plan(f"plan-{index}", waiting=index < PLANS_WAITING), encoding="utf-8"
        )
    # The section every request waits on, declared done so a wait that names it
    # is pending rather than abandoned.
    upstream = json.dumps(
        {
            "s1": "implementable",
            "s2": "implementable",
        },
        separators=(",", ":"),
    ).replace('"', "&quot;")
    (plans / "upstream.html").write_text(
        "<!doctype html>\n"
        f'<meta name="docs-project" content="{PROJECT}">\n'
        '<meta name="reckon-type" content="plan">\n'
        '<meta name="plan-slug" content="upstream">\n'
        '<meta name="plan-status" content="active">\n'
        f'<meta name="plan-section-declarations" content="{upstream}">\n'
        '<h2 id="s2">Upstream</h2>\n',
        encoding="utf-8",
    )
    fixtures._git(repository, "add", "--", "docs")
    fixtures._git(repository, "commit", "-q", "-m", "chore: seed measured plans")
    _populate(repository)
    return repository


def _populate(repository: Path) -> None:
    """Store 80 requests across the plans, then pad the population to 240."""
    base = fixtures._queue(repository)
    for slug in range(PLANS):
        for index in range(REQUESTS_PER_PLAN):
            run_id = f"r-{slug}-{index}"
            pointer = json.loads(json.dumps(base))
            pointer["run_id"] = run_id
            pointer["node"]["id"] = f"node-{slug}-{index}"
            pointer["node"]["goal"] = f"admit node {slug}/{index}"
            pointer["node"]["plan"] = f"plan-{slug}"
            pointer["node"]["section"] = "s1"
            pointer["node"]["write_paths"] = [f"node-{slug}-{index}.txt"]
            pointer["dispatch_options"]["plan_slug"] = f"plan-{slug}"
            if slug < PLANS_WAITING:
                pointer["phase"] = queue_admission.AWAITING_UPSTREAM_PHASE
                pointer["waits"] = [f"plan-{slug}#s2"]
                pointer["wait_projects"] = {f"plan-{slug}#s2": PROJECT}
            queue_admission._write_pointer(run_id, pointer)
    written = PLANS * REQUESTS_PER_PLAN
    for index in range(LIVE_POINTERS - written):
        pointer = json.loads(json.dumps(base))
        pointer["run_id"] = f"r-live-{index}"
        pointer["node"]["id"] = f"live-node-{index}"
        pointer["node"]["write_paths"] = [f"live-node-{index}.txt"]
        pointer["phase"] = "working"
        queue_admission._write_pointer(pointer["run_id"], pointer)


def live_ids() -> list[str]:
    """Every live pointer the pass will list: 80 requests and the padding."""
    return [str(pointer.get("run_id") or "") for pointer in runs.list_live()]


def _receipt() -> dict[str, Any]:
    """The one receipt the pass wrote."""
    path = runs.crew_home() / queue_admission.ADMISSION_VERDICT_LOG
    assert path.exists(), "the pass left no receipt"
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 1, lines
    return json.loads(lines[0])


def stage_reads(receipt: dict[str, Any], stage: str) -> list[dict[str, Any]]:
    """The named reads one stage of a receipt carries."""
    reads = receipt.get("reads")
    assert isinstance(reads, dict), receipt.keys()
    rows = reads.get(stage)
    assert isinstance(rows, list), (stage, sorted(reads))
    assert rows, f"{stage} names no read"
    return rows


def stage_seconds(receipt: dict[str, Any], stage: str) -> float:
    """The time the receipt records for one stage."""
    key = {"release": "release_seconds", "wait": "waits_seconds"}[stage]
    return float(receipt[key] or 0.0)


def sums_within(stage_time: float, rows: list[dict[str, Any]]) -> bool:
    """Whether the named times account for the stage within ``SUMS_WITHIN``."""
    total = sum(float(row["seconds"]) for row in rows)
    if stage_time <= 0.0:
        return total == 0.0
    return abs(stage_time - total) <= SUMS_WITHIN * stage_time


def _drive(monkeypatch: pytest.MonkeyPatch, lane_document: Path) -> dict[str, Any]:
    # The release stage runs at most once per interval, and the interval is held
    # in this process: a pass in a temporary home starts from no earlier tick.
    queue_admission._RELEASE_TICK_LAST.clear()
    _config, resolve = fixtures._config_with_lane(lane_document)
    # No worker slots: every request is held, so the pass decides on all 80 and
    # the two timed stages are what the receipt has to account for.
    fixtures._write_lane(lane_document, fixtures._session_lane(0.0))
    return queue_admission.admit_until_stopped(
        once=True,
        launcher=fixtures._launcher([]),
        config_for=resolve,
        # The receipt is written for a pass that decided something; this pass
        # decides every request still waits on its section, so the landing it
        # started is what its line reports.
        lander=lambda: {"skipped": ["no run to land"], "seconds": 0.0},
    )


def test_one_pass_names_the_reads_of_its_release_and_wait_stages(
    populated: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = tmp_path / "lane.json"
    population = len(live_ids())
    assert population == LIVE_POINTERS, population

    _drive(monkeypatch, lane)

    receipt = _receipt()
    assert receipt["release_seconds"], "the release stage ran"
    assert receipt["waits_seconds"] is not None
    assert receipt["waits_seconds"] is not None
    for stage in ("release", "wait"):
        rows = stage_reads(receipt, stage)
        for row in rows:
            assert isinstance(row.get("read"), str) and row["read"], row
            assert int(row["count"]) > 0, row
            assert float(row["seconds"]) >= 0.0, row
        # Each read is reported with its count and its time, and the times sum
        # to the stage's own time: a receipt whose reads did not add up would
        # name a read that took no time, or a stage whose time came from
        # nowhere.
        assert sums_within(stage_seconds(receipt, stage), rows), (
            stage,
            stage_seconds(receipt, stage),
            rows,
        )
        # Heaviest first, so the receipt's largest read is its first row.
        seconds = [float(row["seconds"]) for row in rows]
        assert seconds == sorted(seconds, reverse=True), seconds

    named = {
        str(row["read"])
        for stage in ("release", "wait")
        for row in stage_reads(receipt, stage)
    }
    assert "live pointers" in named, named
    assert named & {"plan wait refs", "readiness rows", "plan readiness"}, named


def test_a_receipt_with_one_read_omitted_fails_the_sum_check(
    populated: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = tmp_path / "lane.json"
    _drive(monkeypatch, lane)
    receipt = _receipt()
    for stage in ("release", "wait"):
        rows = stage_reads(receipt, stage)
        stage_time = stage_seconds(receipt, stage)
        assert sums_within(stage_time, rows), (stage, stage_time, rows)
        # The same receipt with one read omitted: the stage's time no longer
        # reconciles, so the sum check is one that can fail.
        assert not sums_within(stage_time, rows[1:]), (stage, stage_time, rows)