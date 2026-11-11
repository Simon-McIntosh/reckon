"""A held request names the hold it is under, by kind and id.

Four dispatches of a plan's sections returned ``awaiting-upstream`` with one
reason — "this section's waits have not landed" — for sections that declared no
waits at all. What held them was an open decision on the plan, so a coordinator
reading the answer went looking for an upstream that did not exist.

These cases pin the four facts that fix it. A hold caused by an open decision
reports the decision's key, in the dispatch answer and on the record the answer
left behind. A hold caused by a closed gate reports the gate's id. A hold on a
declared section wait still names that wait's ref, which is the one case where
the wording about waits is true. And an admission pass records a verdict row for
a request held on its section's readiness, naming the blocker there too.
"""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import queue_admission, runs
from reckon.crew.dispatch_admission import LaneHeld
from reckon.crew.node import TaskNode
from tests import test_a_queued_request_waits_on_its_dependencies as fixtures

dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = fixtures.PROJECT
PLAN = fixtures.PLAN
SESSION = fixtures.SESSION
DECISION = "route"
GATE_PLAN_SECTION = f"{PLAN}#s1"


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return fixtures.chain_repo.__wrapped__(tmp_path, monkeypatch)


def _author(
    repository: Path, block: str, *, waits: dict[str, list[str]] | None
) -> None:
    """Add authored holds to the fixture's plan and commit them."""
    path = repository / "docs" / "plans" / f"{PLAN}.html"
    text = path.read_text(encoding="utf-8")
    if waits is not None:
        declaration = json.dumps(waits).replace('"', "&quot;")
        text = re.sub(
            r'name="plan-section-depends-on" content="[^"]*"',
            f'name="plan-section-depends-on" content="{declaration}"',
            text,
            count=1,
        )
    path.write_text(text + block, encoding="utf-8")
    fixtures._run(repository, "add", "--", str(path.relative_to(repository)))
    fixtures._run(repository, "commit", "-q", "-m", "chore: author the hold")


DECISION_BLOCK = (
    '<section data-reckon="decisions"><article class="r-dec" '
    f'data-key="{DECISION}" data-status="open">'
    '<h4 class="r-dec-question">which route?</h4></article></section>\n'
)


def _gate_block(gate_id: str) -> str:
    # A plan carrying execution gates is dispatchable only while it carries an
    # open followup, so the gated fixture carries one.
    return (
        '<section data-reckon="followups"><article class="r-fu" '
        'data-id="f-next" data-status="open">'
        '<h4 class="r-fu-title">carry the work</h4></article></section>\n'
        '<section data-reckon="gates"><div class="r-gate" '
        f'data-id="{gate_id}" data-status="open" data-verdict="" '
        f'data-gating-plan="{GATE_PLAN_SECTION}">'
        '<h4 class="r-gate-measure">the section lands green</h4></div></section>\n'
    )


def _node(section: str = "s2", node_id: str = "n-held") -> TaskNode:
    return TaskNode(
        id=node_id,
        goal=f"build {section}",
        plan=PLAN,
        section=section,
        done_when=f"pytest reports passed for {section}",
        write_paths=[f"{section}.txt"],
        time_budget="20m",
        spec_level="exact",
    )


def _dispatch(repository: Path, section: str = "s2") -> LaneHeld:
    """The hold a dispatch of ``section`` answers with."""
    with pytest.raises(LaneHeld) as caught:
        dispatch_module.dispatch(
            node=_node(section),
            project=PROJECT,
            repo=str(repository),
            config=fixtures.CONFIG,
            session=SESSION,
            local=True,
            backend_override="clive",
            watch_required=False,
            watch_override=True,
        )
    return caught.value


def _queued(repository: Path) -> dict[str, Any]:
    pointers = [dict(pointer) for pointer in runs.list_live(project=PROJECT)]
    pointers = [
        pointer
        for pointer in pointers
        if str(pointer.get("phase") or "") in queue_admission.WAITING_UPSTREAM_PHASES
    ]
    assert len(pointers) == 1, pointers
    return dict(pointers[0])


def _pass(repository: Path) -> dict[str, Any]:
    return queue_admission.admit_once(
        config_for=lambda _pointer: fixtures.CONFIG,
        launcher=lambda *_a, **_k: 0,
    )


def test_a_dispatch_held_by_an_open_decision_names_the_decision(
    repo: Path,
) -> None:
    """The case that started this: no wait exists, an open decision does."""
    _author(repo, DECISION_BLOCK, waits={})

    held = _dispatch(repo)

    assert held.queued["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE
    assert f"decision:{DECISION}" in held.queued["hold"], held.queued
    assert DECISION in held.queued["reason"], held.queued["reason"]
    assert "waits have not landed" not in held.queued["reason"], (
        f"the answer says an upstream is missing: {held.queued['reason']!r}"
    )
    assert DECISION in str(held), str(held)

    pointer = _queued(repo)
    recorded = queue_admission.recorded_pass_hold(pointer)
    assert recorded.get("verdict") == "held-on-readiness", recorded
    assert f"decision:{DECISION}" in recorded.get("hold", ()), recorded
    assert DECISION in str(recorded.get("reason") or ""), recorded
    assert "waits have not landed" not in str(recorded.get("reason") or "")


def test_a_dispatch_held_by_a_closed_gate_names_the_gate(repo: Path) -> None:
    _author(repo, _gate_block("ship"), waits={})

    held = _dispatch(repo)

    assert "gate:ship" in held.queued["hold"], held.queued
    assert "gate:ship" in held.queued["reason"], held.queued["reason"]
    assert "waits have not landed" not in held.queued["reason"]

    pointer = _queued(repo)
    recorded = queue_admission.recorded_pass_hold(pointer)
    assert "gate:ship" in recorded.get("hold", ()), recorded


def test_a_dispatch_held_by_a_declared_wait_names_that_wait(repo: Path) -> None:
    """The one case where the wording about waits is true."""
    _author(repo, "", waits={"s2": [GATE_PLAN_SECTION]})

    held = _dispatch(repo)

    assert held.queued["hold"] == [GATE_PLAN_SECTION], held.queued
    assert held.queued["reason"] == "this section's waits have not landed"
    recorded = queue_admission.recorded_pass_hold(_queued(repo))
    assert recorded.get("verdict") == "held-on-waits", recorded


def test_the_admission_verdict_row_of_the_decision_case_names_it(repo: Path) -> None:
    _author(repo, DECISION_BLOCK, waits={})
    _dispatch(repo)

    result = _pass(repo)

    rows = [
        row
        for row in result.get("verdicts") or ()
        if row.get("verdict") == "held-on-readiness"
    ]
    assert rows, result.get("verdicts")
    assert all(f"decision:{DECISION}" in row.get("hold", ()) for row in rows), rows
    assert any(DECISION in str(row.get("detail") or "") for row in rows), rows


def _readiness_refs(pointer: dict[str, Any]) -> list[str]:
    return [
        ref
        for ref in queue_admission.section_waits_for(pointer)
        if ref.startswith(queue_admission.READINESS_REF_PREFIX)
    ]


def test_a_pass_keeps_the_readiness_hold_a_dispatch_composes(repo: Path) -> None:
    """The pass re-reads waits; the ref the dispatch composed must survive it."""
    _author(repo, DECISION_BLOCK, waits={})
    _dispatch(repo)
    assert _readiness_refs(_queued(repo)) == [
        f"{queue_admission.READINESS_REF_PREFIX}{PROJECT}:{PLAN}#s2"
    ]

    result = _pass(repo)

    assert not result.get("launched"), result.get("launched")
    assert _readiness_refs(_queued(repo)), "the pass released a held request"
