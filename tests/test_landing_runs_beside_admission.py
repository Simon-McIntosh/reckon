"""Landing runs beside the admission pass, one bounded action at a time.

The admission service launches and releases on its own clock whatever the
landing pass is doing: a landing action lives in a worker thread under its own
lock, so a slow merge or a slow suite holds only the next landing. Each pass
records how long its stages took, and a landing step that runs past the bound
the measure configures is stopped and refused rather than allowed to stall the
service.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from reckon import _plan_html
from reckon.crew import landing_measure, queue_admission, queue_landing, runs
from reckon.crew import review as review_module
from tests import test_a_landed_section_releases_its_waiters as release_waits
from tests import test_a_queued_request_waits_on_its_dependencies as waits
from tests import test_the_landing_pass_measures_and_lands as landing_tests
from tests.test_the_landing_pass_measures_and_lands import (
    RUN,
)
from tests.test_the_landing_pass_measures_and_lands import delivery as _delivery

PROJECT = release_waits.PROJECT

# The landing suite's own helpers, so a case here seeds a delivery the way the
# suite that owns that fixture does rather than copying it.
_delivery_accept = landing_tests._accept
_delivery_more = landing_tests._deliver_another
_delivery_stub = landing_tests._stub


@pytest.fixture()
def delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The delivered run's repository, plan and live pointer, from the landing suite."""
    return _delivery.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture()
def chain_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The dependency chain's checkout, built under a temporary crew home."""
    return waits.chain_repo.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A crew home with nothing in it, for a pass that touch no request."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _reset_release_clock() -> None:
    queue_admission._RELEASE_TICK_LAST["at"] = 0.0


def _accept_the_delivery(head: str) -> None:
    """Store a review that accepts the delivered head."""
    base = str(runs.read_pointer(release_waits.RUN_ID).get("base_sha") or "")
    parsed = review_module.parse_review(
        "\n".join(
            f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
        )
    )
    parsed.update(
        {
            "project": PROJECT,
            "reviewed_run_id": release_waits.RUN_ID,
            "review_run_id": "review-of-delivery",
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
        }
    )
    review_module.store_review(parsed)


def _drive_one_pass(monkeypatch: pytest.MonkeyPatch, **options) -> dict:
    """Run the service's loop for exactly one pass and return what it recorded.

    The stop is injected at the pause that ends a pass, so the pass under test
    has completed — released, admitted and recorded — before the loop leaves.
    The loop runs in a thread of its own, so a case can tell a pass that
    finished from one that is still waiting: a caller that joined the loop
    forever would report neither.
    """
    recorded: list[dict] = []

    def admit_once(**_options):
        recorded.append({"admitted": True})
        return {"verdicts": [], "gate": {"state": "open"}}

    def pause(_seconds: float) -> None:
        raise KeyboardInterrupt

    def record(result, **kwargs):
        recorded.append(dict(result))

    monkeypatch.setattr(queue_admission, "admit_once", admit_once)
    monkeypatch.setattr(queue_admission, "_pause", pause)
    monkeypatch.setattr(queue_admission, "_record_pass_verdicts", record)

    done = threading.Event()
    state: dict = {}

    def drive() -> None:
        state["loop"] = queue_admission.admit_until_stopped(interval=0.0, **options)
        done.set()

    thread = threading.Thread(target=drive, daemon=True)
    thread.start()
    return {"done": done, "state": state, "records": recorded}


def test_an_admission_pass_completes_while_a_landing_is_blocked(
    chain_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pass completes and releases its waiter with the landing still held."""
    _reset_release_clock()
    waits._queue_chain(chain_repo)
    # The head lands by a hand merge of the plan: the release tick is what
    # moves the waiter, not a promotion.
    waits._declare(chain_repo, "s1")
    head = release_waits._promotable_run(chain_repo, section="s1")
    _accept_the_delivery(head)
    assert waits._phase_of(chain_repo, "s2") == queue_admission.AWAITING_UPSTREAM_PHASE

    entered = threading.Event()
    released = threading.Event()

    def blocked_land(run_id: str) -> tuple[bool, str]:
        entered.set()
        released.wait(timeout=60.0)
        return True, ""

    monkeypatch.setattr(queue_landing, "_land", blocked_land)

    driven = _drive_one_pass(monkeypatch)

    try:
        assert entered.wait(timeout=5.0), "the landing step was never reached"
        # The pass completes while the landing is still held: the admission
        # loop does not wait for it.
        assert driven["done"].wait(timeout=10.0), "the pass waited for the landing"
        assert driven["state"]["loop"] == {"passes": 1}, driven["state"]
        assert any(record.get("admitted") for record in driven["records"])
        assert not released.is_set(), "the pass waited for the landing"
        assert waits._phase_of(chain_repo, "s2") == queue_admission.QUEUED_PHASE
    finally:
        released.set()


def test_a_landing_tick_lands_one_delivery_and_leaves_the_rest(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three accepted deliveries take three ticks, one land step each."""
    _delivery_more("r-second", order=1)
    _delivery_more("r-third", order=2)
    for run_id in (RUN, "r-second", "r-third"):
        _delivery_accept(
            base=str(delivery["base"]), head=str(delivery["head"]), run_id=run_id
        )

    land_calls: list[str] = []

    def fake_land(run_id: str) -> tuple[bool, str]:
        land_calls.append(run_id)
        runs.pointer_path(run_id).unlink()
        return True, ""

    monkeypatch.setattr(queue_landing, "_land", fake_land)
    monkeypatch.setattr(queue_landing, "_landing_beat", lambda *a, **k: "")
    monkeypatch.setattr(
        landing_measure,
        "_RUN",
        _delivery_stub([], suite_head=(0, "1 passed\n"), isolated={}),
    )

    first = queue_landing.land_once()
    assert first["landed"] == [RUN], first
    assert first["deferred"] == ["r-second", "r-third"], first
    assert land_calls == [RUN]

    second = queue_landing.land_once()
    assert second["landed"] == ["r-second"], second
    assert second["deferred"] == ["r-third"], second
    assert land_calls == [RUN, "r-second"]

    third = queue_landing.land_once()
    assert third["landed"] == ["r-third"], third
    assert land_calls == [RUN, "r-second", "r-third"]


def test_a_land_step_past_its_bound_is_stopped_and_refused(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A step that outruns the landing measure's timeout is killed and named."""
    _delivery_accept(base=str(delivery["base"]), head=str(delivery["head"]))
    bound = 0.25
    monkeypatch.setattr(queue_landing, "_land_command", lambda run_id: ["sleep", "5"])
    monkeypatch.setattr(queue_landing, "_measure_timeout", lambda pointer: bound)
    monkeypatch.setattr(queue_landing, "_landing_beat", lambda *a, **k: "")
    monkeypatch.setattr(
        landing_measure,
        "_RUN",
        _delivery_stub([], suite_head=(0, "1 passed\n"), isolated={}),
    )

    started = time.monotonic()
    result = queue_landing.land_once()
    elapsed = time.monotonic() - started

    assert result["landed"] == [], result
    assert len(result["obligations"]) == 1, result
    guard = result["obligations"][0]["guard"]
    assert f"{bound:g} s bound" in guard, guard
    # The step was stopped at the bound rather than allowed to run its five
    # seconds out.
    assert elapsed < 4.0, elapsed
    recorded = runs.read_pointer(RUN)["admission_landing"]
    assert recorded["status"] == "refused"
    assert recorded["guard"] == guard


def test_each_pass_record_carries_its_stage_timings(
    crew_home: Path, monkeypatch
) -> None:
    """A pass's row names its start, its end and each stage's seconds."""
    _reset_release_clock()

    def lander() -> dict:
        return {"landed": [], "obligations": [], "deferred": []}

    driven = _drive_one_pass(monkeypatch, lander=lander)
    assert driven["done"].wait(timeout=10.0), "the pass did not finish"
    rows = [record for record in driven["records"] if "started_at" in record]
    assert rows, driven["records"]
    row = rows[0]
    assert row["started_at"] and row["ended_at"], row
    for stage in ("release_seconds", "launch_seconds", "land_seconds"):
        assert isinstance(row[stage], float) and row[stage] >= 0.0, row
    # The landing action reports its own start, end and duration.
    landing = row["landing"]
    assert landing["started_at"] and landing["ended_at"], landing
    assert isinstance(landing["seconds"], float), landing


def test_a_raising_landing_is_restarted_and_never_stops_the_loop(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A landing that raises is recorded, and the next tick starts a new one."""
    attempts: list[int] = []

    def raising() -> dict:
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise RuntimeError("the landing script vanished")
        return {"landed": ["r-recovered"], "obligations": [], "deferred": []}

    worker = queue_admission._LandingWorker(raising)
    assert worker.tick() is True
    first = worker.settle(timeout=5.0)
    assert "the landing script vanished" in first["error"], first
    assert worker.busy is False

    assert worker.tick() is True
    second = worker.settle(timeout=5.0)
    assert second["landed"] == ["r-recovered"], second

    # The service loop survives a landing that raises on every tick: it runs
    # its passes, and each pass's record carries the failure.
    _reset_release_clock()

    def always_raising() -> dict:
        raise RuntimeError("the landing script vanished")

    driven = _drive_one_pass(monkeypatch, lander=always_raising)
    assert driven["done"].wait(timeout=10.0), "the pass did not finish"
    assert driven["state"]["loop"]["passes"] >= 1, driven["state"]["loop"]


def test_the_land_step_waives_the_impl_move_the_beat_makes(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run whose section is still implementable lands, and is ticked done.

    ``crew complete`` refuses a passing implement run whose plan impl has not
    moved, and the landing beat that advances the section runs only after the
    merge, so the step must record the reason it lands ahead of that move.
    """
    _delivery_accept(base=str(delivery["base"]), head=str(delivery["head"]))
    state = _plan_html.read_state(delivery["plan"].read_text(encoding="utf-8"))
    assert state["section_declarations"]["s1"] == "implementable"

    commands: list[list[str]] = []

    def run_step(command, **_options):
        commands.append(list(command))
        return subprocess.CompletedProcess(command, 0, json.dumps({"ok": True}), "")

    monkeypatch.setattr(queue_landing, "_run_srun", run_step)
    monkeypatch.setattr(
        landing_measure,
        "_RUN",
        _delivery_stub([], suite_head=(0, "1 passed\n"), isolated={}),
    )

    result = queue_landing.land_once()

    assert result["landed"] == [RUN], result
    command = commands[0]
    assert "--no-impl-change" in command, command
    reason = command[command.index("--no-impl-change") + 1]
    assert reason == queue_landing.LANDING_BEAT_REASON
    assert command[-1] == "--land", command
    state = _plan_html.read_state(delivery["plan"].read_text(encoding="utf-8"))
    assert state["section_declarations"]["s1"] == "done", state["section_declarations"]
