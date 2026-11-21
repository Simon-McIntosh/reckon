"""One refusal does not park a queued request whose contract is stored.

An admission pass reads a queued request's node body from the request's own run
directory: the authority, the measure and the brief live in ``node.json`` there.
A read that comes back empty — the body file not yet written, or a tree moving
under a landing, or any other transient — makes the launch a contract refusal
naming no authority and no measure, and the refusal is stamped on the pointer.
The pass before this file read that stamp as a finding to act on, so a single
refusal raised a ``queued-contract-refused`` row whose remedy changed nothing
the refusal named, and the request sat in the queue while its stored contract
was perfectly intact.

The contract a refusal names is the request's *stored* one, so a refusal is
evidence about the pass's read, not about the request, and one reading proves
nothing: the next pass reads the request afresh. Two passes that each read the
request and each refuse it agree, and only then is the request parked — its row
naming the property that failed and the command that sets it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import queue_admission, runs
from tests.test_a_queued_run_is_admitted import (  # noqa: F401 - fixtures
    _config_for,
    _lane_document,
    _launcher,
    admit_repo,
)
from tests.test_a_queued_run_is_admitted import (
    _queue as _queue_implementation,
)

PROJECT = "proj"
SESSION = "coordinator-a"
BRIEF_TEXT = "Review the plan and record one verdict per section.\n"


def _queue_review(repository: Path, *, run_id: str) -> dict:
    """One composed plan review, queued with the brief and measure it reads.

    A composed review's authority is its brief, not a plan section, so its node
    names no plan and no section and its contract lives in the body file the
    queued pointer carries the path to.
    """
    from reckon.crew.node import TaskNode

    brief = repository / "briefs" / f"{run_id}.md"
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text(BRIEF_TEXT, encoding="utf-8")
    node = TaskNode(
        id=f"plan-review-of-plan-a-{run_id}",
        goal="review plan-a and record a verdict for every section",
        plan="",
        section="",
        brief=str(brief),
        role="review",
        done_when="pytest tests/test_a_stored_contract_is_not_parked_by_one_refusal.py reports one verdict per section of plan-a",
        write_paths=[f"{run_id}-report.txt"],
        time_budget="20m",
        spec_level="exact",
    )
    joined = datetime.now(UTC).isoformat()
    return {
        "run_id": run_id,
        "project": PROJECT,
        "session": json.loads(json.dumps(SESSION)),
        "repo": str(repository),
        "node": node.as_dict(),
        "backend": "clive",
        "launch": "cli",
        "local": True,
        "phase": "queued",
        "queued_at": joined,
        "created_at": joined,
        "hold": {"held": True},
        "dispatch_options": {
            "project": PROJECT,
            "plan_slug": "plan-a",
            "section": "",
            "session": SESSION,
            "local": True,
            "no_watch": True,
            "overrides": [],
        },
    }


def _queue_pair(repository: Path) -> None:
    """Queue an implementation request and a composed plan review, contracts stored."""
    runs.queue_dispatch(
        _queue_implementation(repository, node_id="stored-node", run_id="r-stored")
    )
    runs.queue_dispatch(_queue_review(repository, run_id="r-reviewed"))


def _contract_gate(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """Let one pass read no stored node body, as a transient read would.

    The queued request's authority and measure live in the body file its pointer
    names, and a pass reads them through this one reader. Answering an empty
    mapping for one pass is the measured condition: a pass that read no contract
    for a request whose contract was stored.
    """
    gate = {"drop": False}
    real = runs._read_node_body_file

    def read(path: Path) -> dict:
        if gate["drop"]:
            return {}
        return real(path)

    monkeypatch.setattr(runs, "_read_node_body_file", read)
    return gate


def _records(project: str = PROJECT) -> list[dict]:
    from reckon.crew import obligations as obligations_module

    return list(obligations_module.obligations(project, SESSION)["obligations"])


def _refusal_rows(project: str = PROJECT) -> list[dict]:
    from reckon.crew import obligations as obligations_module

    return [
        row
        for row in _records(project)
        if row["kind"] == obligations_module.QUEUED_CONTRACT_REFUSED_DUTY_KIND
    ]


def _one_pass(config, resolve, recorded: list) -> dict:
    return queue_admission.admit_once(
        config_for=resolve, launcher=_launcher(recorded), lock=True
    )


def _two_slot_lane(tmp_path: Path):
    """The fixture's own config, with a review role and room for both requests."""
    config, resolve = _config_for()
    config["roles"]["review"] = {}
    config["backends"]["clive"]["lane_document"] = str(
        _lane_document(tmp_path, worker_slots=2)
    )
    return config, resolve


def test_a_transient_contract_read_does_not_park_the_request(
    admit_repo: Path,  # noqa: F811 - imported fixture
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pass that reads no contract leaves the stored one standing for the next."""
    config, resolve = _two_slot_lane(tmp_path)
    _queue_pair(admit_repo)
    gate = _contract_gate(monkeypatch)
    recorded: list = []

    gate["drop"] = True
    refused_pass = _one_pass(config, resolve, recorded)

    assert refused_pass["launched"] == [], refused_pass
    # One refusal is a reading of the pass, not a finding for the session.
    assert _refusal_rows() == []
    assert sorted(refused_pass["failed"]) == ["r-reviewed", "r-stored"], refused_pass
    assert runs.read_pointer("r-stored")["phase"] == queue_admission.QUEUED_PHASE

    gate["drop"] = False
    next_pass = _one_pass(config, resolve, recorded)

    assert sorted(next_pass["launched"]) == ["r-reviewed", "r-stored"], next_pass
    for run_id in ("r-stored", "r-reviewed"):
        assert runs.read_pointer(run_id)["phase"] != queue_admission.QUEUED_PHASE
    assert _refusal_rows() == []


def _strip_done_when(run_id: str) -> None:
    """Leave the request's stored contract with no measure at all."""
    body_path = runs.node_body_path(run_id)
    body = json.loads(body_path.read_text(encoding="utf-8"))
    body.pop("done_when", None)
    body_path.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")


def test_a_measureless_contract_is_parked_only_when_two_passes_agree(
    admit_repo: Path,  # noqa: F811 - imported fixture
    tmp_path: Path,
) -> None:
    """Two reads that agree park the request; the row names the field to set."""
    config, resolve = _two_slot_lane(tmp_path)
    runs.queue_dispatch(
        _queue_implementation(admit_repo, node_id="no-measure", run_id="r-no-measure")
    )
    _strip_done_when("r-no-measure")
    recorded: list = []

    first = _one_pass(config, resolve, recorded)

    assert sorted(first["failed"]) == ["r-no-measure"], first
    assert _refusal_rows() == []
    assert runs.read_pointer("r-no-measure")["phase"] == queue_admission.QUEUED_PHASE

    second = _one_pass(config, resolve, recorded)

    assert sorted(second["failed"]) == ["r-no-measure"], second
    parked = _refusal_rows()
    assert len(parked) == 1, parked
    assert "demonstrable" in parked[0]["reason"], parked[0]
    assert "done-when" in parked[0]["reason"], parked[0]
    assert "--done-when" in parked[0]["next_command"], parked[0]

    third = _one_pass(config, resolve, recorded)

    assert _refusal_rows() == parked
    assert third["failed"] == [], third
    assert "r-no-measure" in third["held"], third


def _restore_done_when(run_id: str, measure: str) -> None:
    body_path = runs.node_body_path(run_id)
    body = json.loads(body_path.read_text(encoding="utf-8"))
    body["done_when"] = measure
    body_path.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")


def test_repairing_the_measure_releases_the_parked_request(
    admit_repo: Path,  # noqa: F811 - imported fixture
    tmp_path: Path,
) -> None:
    """A parked refusal is released by changing the field its row names."""
    config, resolve = _two_slot_lane(tmp_path)
    runs.queue_dispatch(
        _queue_implementation(admit_repo, node_id="repaired", run_id="r-repaired")
    )
    _strip_done_when("r-repaired")
    recorded: list = []
    for _ in range(2):
        _one_pass(config, resolve, recorded)
    arrived = _one_pass(config, resolve, recorded)
    assert "r-repaired" in arrived["held"], arrived
    assert len(_refusal_rows()) == 1

    _restore_done_when(
        "r-repaired",
        "pytest tests/test_a_stored_contract_is_not_parked_by_one_refusal.py reports passed",
    )

    after = _one_pass(config, resolve, recorded)

    assert "r-repaired" in after["launched"], after
    assert _refusal_rows() == []
