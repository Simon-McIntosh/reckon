"""The stop hook, the live view and the drain report one stuck verdict.

A request whose wait can never resolve read four ways: the obligation its
pointer carried, the stop hook saying "waiting upstream", the live view saying
"wait for ... to land", and the drain view alone knowing it was stuck. This
module pins the one verdict the three surfaces now read, and the two cases the
drain view's old derivation did not carry: a wait no project can resolve, and a
request left admissible with lane room across three admission passes without
launching.

The fleet is built from live pointers and admission pass records, which is what
the three readers consume; no worker is launched and no plan is edited.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from reckon.crew import (
    obligation_snapshot,
    queue_admission,
    recovery_classification,
    runs,
)
from reckon.crew.runs import _write_json
from reckon.hooks import coordinator_obligations as hook
from tests import test_a_queued_request_waits_on_its_dependencies as fixtures

PROJECT = fixtures.PROJECT
SESSION = fixtures.SESSION
chain_repo = fixtures.chain_repo

#: Two hours: well past the drain view's own one-hour bound.
OLD_SECONDS = 1.6 * 3600
UNRESOLVABLE = (
    "wait-unresolvable: nowhere#gone - the wait names no project or run it lands in"
)


def _iso(age_seconds):
    """An instant that many seconds in the past, in the pointer's own form."""
    return (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()


def _queue(repository, run_ids):
    """Queue one request per run id on the dependency-free section.

    The requests are made the way the fleet makes them, so the snapshot's own
    session slice lists them: a pointer written by hand carries no request
    record, and a reader that composes the pane's payload skips it.
    """
    nodes = [
        fixtures._node(
            run_id.removeprefix("r-"),
            "s1",
            manifest_path=str(repository / "manifest.md"),
        )
        for run_id in run_ids
    ]
    stored = queue_admission.queue_requests(
        nodes,
        project=PROJECT,
        session=SESSION,
        repo_root=repository,
        config=fixtures.CONFIG,
        run_ids={node.id: run_id for node, run_id in zip(nodes, run_ids, strict=True)},
    )
    for run_id in run_ids:
        assert stored[run_id]["phase"] == queue_admission.QUEUED_PHASE, stored[run_id]
    return stored


def _pointer(repository, run_id, *, phase, age_seconds, **extra):
    """Set a queued pointer's phase and stamps as its pass leaves them."""
    path = runs.pointer_path(run_id)
    record = dict(runs.read_pointer(run_id)) if path.is_file() else {}
    record.update(
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "phase": phase,
            "repo": str(repository),
            "created_at": _iso(age_seconds),
            "queued_at": _iso(age_seconds),
        }
    )
    record.update(extra)
    _write_json(path, record)
    return record


def _write_passes(records):
    """Write the admission pass log the three readers share."""
    path = runs.crew_home() / runs.ADMISSION_VERDICT_LOG_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )


def _admissible_pass(run_id, verdict):
    """One pass whose own record says it had lane room it did not use."""
    return {
        "at": datetime.now(UTC).isoformat(),
        "admissible_age_seconds": 900.0,
        "pointers": [{"run_id": run_id, "verdict": verdict, "stage": "launch"}],
    }


def _payload(run_ids):
    """The payload the stop hook renders, holding one queued duty per request.

    The duty is composed where the hook meets it: a queued row's item, the same
    item the published slice composes, with the stuck verdict applied by the one
    helper both producers call. This checkout's fleet-state sweep reads a
    pointer with no worker record as an in-harness run and so drops the row
    before it becomes a duty, which a synthetic queued pointer always is; the
    hook's own formatter is then asked to render what it is given.
    """
    items = []
    for run_id in run_ids:
        pointer = runs.read_pointer(run_id)
        waits = [str(ref) for ref in pointer.get("waits") or ()]
        waiting_upstream = pointer.get("phase") in (
            queue_admission.WAITING_UPSTREAM_PHASES
        )
        item = {
            "kind": "run-queued",
            "run_id": run_id,
            "node": run_id.removeprefix("r-"),
            "plan": fixtures.PLAN,
            "age_seconds": 0,
            "next_command": (

                    "waiting upstream: " + ", ".join(waits)
                    if waiting_upstream and waits
                    else "wait for a local lane slot"

            ),
        }
        items.append(obligation_snapshot.apply_stuck_verdict(item, pointer))
    return {"project": PROJECT, "session": SESSION, "obligations": items}
    return {"project": PROJECT, "session": SESSION, "obligations": items}


def _without_age(text):
    """A reason with its measured age removed, for comparing two readings.

    Each surface reads the clock when it is asked, so two readings taken a
    minute apart report the same verdict with a different age. The verdict and
    what it names are the agreement; the age is a measurement of the moment.
    """
    return text.split(" for ")[0]


def _hook_text(payload):
    """The checklist the stop hook emits for this payload."""
    return hook.format_checklist(payload, limit_resets=[])


def _hook_item(run_id, payload):
    """The duty the stop hook formats: the item behind its line for this run."""
    items = [
        item
        for item in payload["obligations"]
        if str(item.get("run_id") or "") == run_id
    ]
    assert len(items) == 1, payload["obligations"]
    return items[0]


def _live_row(run_id):
    """The row `crew view live` draws of one pointer."""
    return recovery_classification.classify_pointer(runs.read_pointer(run_id))


def _drain_rows():
    """The rows the drain view reports stuck, keyed by run id."""
    week = runs.drain(PROJECT)["week"]["project"]
    return {str(row["run_id"]): row for row in week["stuck"]}


@pytest.fixture
def quiet_plan_reads(monkeypatch):
    """The drain view's plan states are not what these cases measure."""
    monkeypatch.setattr(runs, "_drain_plan_states", lambda _project: [])
    monkeypatch.setattr(runs, "_unloaded_sections", lambda *_args: {})
    return monkeypatch


def test_the_three_surfaces_agree_on_which_requests_are_stuck_and_why(
    chain_repo, quiet_plan_reads
):
    """The old stuck case, both new cases, and one request merely waiting."""
    _queue(
        chain_repo,
        ["r-old", "r-unresolvable", "r-admissible", "r-waiting"],
    )
    _pointer(chain_repo, "r-old", phase="queued", age_seconds=OLD_SECONDS)
    _pointer(
        chain_repo,
        "r-unresolvable",
        phase="awaiting-upstream",
        age_seconds=OLD_SECONDS,
        waits=["nowhere#gone"],
        waits_at=_iso(OLD_SECONDS),
        upstream_obligations=[UNRESOLVABLE],
    )
    _pointer(chain_repo, "r-admissible", phase="queued", age_seconds=OLD_SECONDS)
    _pointer(
        chain_repo,
        "r-waiting",
        phase="awaiting-upstream",
        age_seconds=OLD_SECONDS,
        waits=[fixtures.PLAN + "#s0"],
        waits_at=_iso(OLD_SECONDS),
    )
    _write_passes(
        [
            {
                "at": datetime.now(UTC).isoformat(),
                "pointers": [{"run_id": "r-old", "verdict": "held-for-allowance"}],
            },
            _admissible_pass("r-admissible", "stalled"),
            _admissible_pass("r-admissible", "stalled"),
            _admissible_pass("r-admissible", "stalled"),
        ]
    )

    drain = _drain_rows()
    payload = _payload(
        ["r-old", "r-unresolvable", "r-admissible", "r-waiting"]
    )
    reasons = {run_id: row["reason"] for run_id, row in drain.items()}
    assert "held-for-allowance" in reasons["r-old"], drain
    for run_id in ("r-old", "r-unresolvable", "r-admissible"):
        assert run_id in reasons, drain
        live = _live_row(run_id)
        assert live["stuck"] is True, live
        assert _without_age(live["stuck_reason"]) == _without_age(reasons[run_id]), (
            live,
            reasons[run_id],
        )
        assert drain[run_id]["kind"] in {"waiting", "wait-unresolvable", "admissible-unlaunched"}
        item = _hook_item(run_id, payload)
        assert _without_age(item["next_command"]) == _without_age(
            "stuck: " + reasons[run_id]
        ), item
        assert _without_age(item["stuck_reason"]) == _without_age(reasons[run_id]), item
        # And the hook prints it: the row is printed rather than counted, so the
        # line a coordinator reads carries the same reason the other two show.
        assert item["next_command"] in _hook_text(payload), _hook_text(payload)
    assert "r-waiting" not in reasons, drain
    assert _live_row("r-waiting")["stuck"] is False
    assert _hook_item("r-waiting", payload)["next_command"].startswith(
        "waiting upstream"
    ), _hook_item("r-waiting", payload)


def test_two_admission_passes_are_not_stuck_and_three_are(chain_repo, quiet_plan_reads):
    """A request admissible with lane room is stuck only from the third pass."""
    _queue(chain_repo, ["r-three"])
    _pointer(chain_repo, "r-three", phase="queued", age_seconds=15.0)
    _write_passes(
        [_admissible_pass("r-three", "stalled"), _admissible_pass("r-three", "stalled")]
    )
    assert "r-three" not in _drain_rows()
    assert _live_row("r-three")["stuck"] is False
    assert _hook_item("r-three", _payload(["r-three"]))["next_command"].startswith(
        "wait"
    )

    _write_passes(
        [
            _admissible_pass("r-three", "stalled"),
            _admissible_pass("r-three", "stalled"),
            _admissible_pass("r-three", "stalled"),
        ]
    )
    drain = _drain_rows()
    assert "r-three" in drain, drain
    assert drain["r-three"]["kind"] == "admissible-unlaunched", drain
    live = _live_row("r-three")
    assert live["stuck"] is True, live
    assert _without_age(live["stuck_reason"]) == _without_age(
        drain["r-three"]["reason"]
    ), live


def test_a_request_behind_earlier_ones_is_not_stuck(chain_repo, quiet_plan_reads):
    """A deep queue on a lane that is launching is a wait, not a stuck request."""
    _queue(chain_repo, ["r-behind"])
    _pointer(chain_repo, "r-behind", phase="queued", age_seconds=OLD_SECONDS)
    _write_passes(
        [
            _admissible_pass("r-behind", "held-behind-earlier-request"),
            _admissible_pass("r-behind", "held-behind-earlier-request"),
            _admissible_pass("r-behind", "held-behind-earlier-request"),
        ]
    )
    assert "r-behind" not in _drain_rows()
    assert _live_row("r-behind")["stuck"] is False


