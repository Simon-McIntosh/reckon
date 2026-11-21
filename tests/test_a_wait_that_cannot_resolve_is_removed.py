"""A wait that cannot resolve is removed; an idle wait is reported once.

Every request stored ``awaiting-upstream`` is classed by what could release it:
an upstream that is being built (``progressing``), an upstream that is still
open but that nobody is building (``idle``), or an upstream that can never land
(``stranded``). A stranded request is retired on the first pass that sees it,
with a discard record naming the upstream and why. An idle request is reported
once to its owner after the threshold, and a progressing one is untouched
however long it has waited. Age alone never removes a wait, and a request a
coordinator holds keeps its hold whatever its class.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import obligations as obligations_module
from reckon.crew import promotion, queue_admission, runs
from reckon.crew.admission import verdicts as verdicts_module
from reckon.crew.node import TaskNode
from tests import test_a_queued_request_waits_on_its_dependencies as fixtures

PROJECT = fixtures.PROJECT
SESSION = fixtures.SESSION
PLAN = fixtures.PLAN


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return fixtures.chain_repo.__wrapped__(tmp_path, monkeypatch)


def _node(node_id: str, section: str, **extra) -> TaskNode:
    return fixtures._node(node_id, section, **extra)


def _queue(repo: Path, *nodes: TaskNode, run_ids: dict[str, str]) -> dict:
    return queue_admission.queue_requests(
        list(nodes),
        project=PROJECT,
        session=SESSION,
        repo_root=repo,
        config=fixtures.CONFIG,
        run_ids=run_ids,
    )


def _live() -> list[dict]:
    return runs.list_live(project=PROJECT)


def _set_plan_status(repo: Path, status: str) -> None:
    path = repo / "docs" / "plans" / f"{PLAN}.html"
    text = path.read_text(encoding="utf-8")
    updated = re.sub(
        r'name="plan-status" content="[^"]*"',
        f'name="plan-status" content="{status}"',
        text,
        count=1,
    )
    if updated == text:
        return
    path.write_text(updated, encoding="utf-8")
    fixtures._run(repo, "add", "--", f"docs/plans/{PLAN}.html")
    fixtures._run(repo, "commit", "-q", "-m", f"chore: mark the plan {status}")


def _drop_declaration(repo: Path, section: str) -> None:
    """Remove one section from the plan's declarations, as a plan edit does."""
    path = repo / "docs" / "plans" / f"{PLAN}.html"
    text = path.read_text(encoding="utf-8")
    match = re.search(r'name="plan-section-declarations" content="([^"]*)"', text)
    assert match is not None, "the fixture always writes a declarations meta"
    raw = match.group(1)
    declarations = json.loads(raw.replace("&quot;", '"').replace("&amp;", "&"))
    declarations.pop(section, None)
    updated = text.replace(raw, json.dumps(declarations).replace('"', "&quot;"), 1)
    path.write_text(updated, encoding="utf-8")
    fixtures._run(repo, "add", "--", f"docs/plans/{PLAN}.html")
    fixtures._run(repo, "commit", "-q", "-m", f"chore: drop the {section} declaration")


def _age(pointer: dict, hours: float) -> dict:
    """Rewrite a pointer's arrival stamps to ``hours`` in the past."""
    stale = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    pointer["created_at"] = stale
    pointer["waits_at"] = stale
    runs._write_json(runs.pointer_path(str(pointer["run_id"])), pointer)
    return pointer


def _pointer(run_id: str) -> dict:
    pointer = runs.read_pointer(run_id)
    assert isinstance(pointer, dict), run_id
    return pointer


def _pointer_gone(run_id: str) -> bool:
    return not runs.pointer_path(run_id).exists()


def test_one_request_per_class(repo: Path) -> None:
    # s2 waits on s1. A request for s1 makes the s2 wait productive; a second
    # repo state with no s1 request makes it idle; a third names a section the
    # plan does not carry, which can never land.
    fixtures._declare_wait_refs(repo, section="s2", refs=[f"{PLAN}#s1"])
    _queue(repo, _node("upstream", "s1"), run_ids={"upstream": "r-upstream"})
    _queue(repo, _node("waiting", "s2"), run_ids={"waiting": "r-waiting"})

    queue_admission.advance_waiting_requests(_live())

    assert _pointer("r-waiting")["wait_class"] == "progressing"
    assert _pointer("r-waiting")["phase"] == "awaiting-upstream"


def test_a_wait_500_hours_behind_a_live_upstream_run_is_untouched(repo: Path) -> None:
    fixtures._declare_wait_refs(repo, section="s2", refs=[f"{PLAN}#s1"])
    _queue(repo, _node("upstream", "s1"), run_ids={"upstream": "r-upstream"})
    _queue(repo, _node("waiting", "s2"), run_ids={"waiting": "r-waiting"})
    _age(_pointer("r-waiting"), hours=500)

    outcome = queue_admission.advance_waiting_requests(_live())

    pointer = _pointer("r-waiting")
    assert pointer["wait_class"] == "progressing"
    assert pointer["phase"] == "awaiting-upstream"
    assert "r-waiting" not in [row["run_id"] for row in outcome["retired"]]
    assert pointer.get("idle_reported_at") is None
    assert outcome["obligations"] == []


def test_a_request_whose_upstream_plan_is_superseded_is_retired(repo: Path) -> None:
    fixtures._declare_wait_refs(repo, section="s2", refs=[f"{PLAN}#s1"])
    _queue(repo, _node("waiting", "s2"), run_ids={"waiting": "r-waiting"})
    _set_plan_status(repo, "superseded")

    outcome = queue_admission.advance_waiting_requests(_live())

    assert [row["run_id"] for row in outcome["retired"]] == ["r-waiting"]
    assert "superseded" in outcome["retired"][0]["reason"]
    assert f"{PLAN}#s1" in outcome["retired"][0]["reason"]
    assert _pointer_gone("r-waiting")
    assert promotion.discard_recorded("r-waiting")
    record = json.loads(
        promotion.discard_record_path("r-waiting").read_text(encoding="utf-8")
    )
    assert "superseded" in str(record.get("reason") or "")
    assert f"{PLAN}#s1" in str(record.get("reason") or "")

    # The pass's own record is what tells the owner: the retired request has no
    # pointer left to read, so the retirement travels on the pass record and
    # appears once among the owner's duties.
    pass_record = verdicts_module._pass_verdict_record(
        {"retired": outcome["retired"]}, lane="local", code_stamp="gate"
    )
    assert pass_record is not None
    assert pass_record["retired"] == outcome["retired"]
    log = runs.crew_home() / queue_admission.ADMISSION_VERDICT_LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps(pass_record) + "\n", encoding="utf-8")

    view = obligations_module.obligations(PROJECT, SESSION)
    retired_rows = [
        row for row in view["obligations"] if row["kind"] == "wait-retired-upstream"
    ]
    assert [row["run_id"] for row in retired_rows] == ["r-waiting"]
    assert "superseded" in retired_rows[0]["reason"]


def test_a_request_naming_a_section_its_plan_does_not_carry_is_retired(
    repo: Path,
) -> None:
    fixtures._declare_wait_refs(repo, section="s2", refs=[f"{PLAN}#s1"])
    _queue(repo, _node("waiting", "s2"), run_ids={"waiting": "r-waiting"})
    _drop_declaration(repo, "s1")

    outcome = queue_admission.advance_waiting_requests(_live())

    assert [row["run_id"] for row in outcome["retired"]] == ["r-waiting"]
    assert "carries no section" in outcome["retired"][0]["reason"]
    assert _pointer_gone("r-waiting")

    # A later pass must not rebuild the request it removed, nor record the
    # retirement twice: the discard is the only record of it.
    again = queue_admission.advance_waiting_requests(_live())

    assert again["retired"] == []
    assert _pointer_gone("r-waiting")


def test_an_idle_request_is_reported_once(repo: Path) -> None:
    fixtures._declare_wait_refs(repo, section="s2", refs=[f"{PLAN}#s1"])
    _queue(repo, _node("waiting", "s2"), run_ids={"waiting": "r-waiting"})
    _age(_pointer("r-waiting"), hours=100)

    first = queue_admission.advance_waiting_requests(_live())
    reported = _pointer("r-waiting")
    assert reported["wait_class"] == "idle"
    assert reported["idle_reported_at"]
    assert [row["run_id"] for row in first["obligations"]] == ["r-waiting"]
    assert "nobody is building" in first["obligations"][0]["detail"]

    view = obligations_module.obligations(PROJECT, SESSION)
    idle_rows = [row for row in view["obligations"] if row["kind"] == "wait-idle"]
    assert [row["run_id"] for row in idle_rows] == ["r-waiting"]
    assert "nobody is building" in idle_rows[0]["reason"]

    second = queue_admission.advance_waiting_requests(_live())
    assert second["obligations"] == []
    assert _pointer("r-waiting")["phase"] == "awaiting-upstream"


def test_a_held_request_in_the_stranded_class_is_not_retired(repo: Path) -> None:
    fixtures._declare_wait_refs(repo, section="s2", refs=[f"{PLAN}#s1"])
    _queue(repo, _node("waiting", "s2"), run_ids={"waiting": "r-waiting"})
    _drop_declaration(repo, "s1")
    pointer = _pointer("r-waiting")
    pointer["queue_hold"] = {
        "reason": "the coordinator asked for this to wait",
        "started_at": datetime.now(UTC).isoformat(),
    }
    runs._write_json(runs.pointer_path("r-waiting"), pointer)

    outcome = queue_admission.advance_waiting_requests(_live())

    assert outcome["retired"] == []
    live = _pointer("r-waiting")
    assert live["wait_class"] == "stranded"
    assert live["phase"] == "awaiting-upstream"
    assert not promotion.discard_recorded("r-waiting")


def test_a_stranded_request_of_a_lost_owner_is_retired_the_same_way(repo: Path) -> None:
    fixtures._declare_wait_refs(repo, section="s2", refs=[f"{PLAN}#s1"])
    _queue(repo, _node("waiting", "s2"), run_ids={"waiting": "r-waiting"})
    _drop_declaration(repo, "s1")
    pointer = _pointer("r-waiting")
    pointer["session"] = "coordinator-that-is-gone"
    runs._write_json(runs.pointer_path("r-waiting"), pointer)

    outcome = queue_admission.advance_waiting_requests(_live())

    assert [row["run_id"] for row in outcome["retired"]] == ["r-waiting"]
    assert "carries no section" in outcome["retired"][0]["reason"]
    assert outcome["retired"][0]["session"] == "coordinator-that-is-gone"


def test_a_superseded_plan_read_as_active_does_not_retire(repo: Path) -> None:
    # The negative control: the same fixture with the plan back to active. The
    # retirement case above must fail here, because nothing says the section
    # can never land.
    fixtures._declare_wait_refs(repo, section="s2", refs=[f"{PLAN}#s1"])
    _queue(repo, _node("waiting", "s2"), run_ids={"waiting": "r-waiting"})
    _set_plan_status(repo, "active")

    outcome = queue_admission.advance_waiting_requests(_live())

    assert outcome["retired"] == []
    live = _pointer("r-waiting")
    assert live["phase"] == "awaiting-upstream"
    assert live["wait_class"] == "idle"
    assert not promotion.discard_recorded("r-waiting")
