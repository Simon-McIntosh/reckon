"""A section a run has delivered is not offered again.

A promoted run leaves the live pointer set, so every later wave read a delivered
section as ready and dispatched it a second time. Three readers must agree: the
roadmap's ready set, which section rows carry, and batch dispatch, which answers
a duplicate entry with the run that holds it instead of queueing it. A section
declared done whose run's head never reached the integration branch is reported
as a wiring finding naming that run.

The hold is per node. A section split into several dispatch nodes keeps its
other nodes ready while one node is delivered, and this module's fixture is the
same shape in both cases: what differs is whether a second node's request
exists.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import ledger, roadmap
from reckon.crew import dispatch_batch, runs

PROJECT = "sample"
PLAN = "alpha"

CONFIG = {
    "default_backend": "clang",
    "local_backend": "clang",
    "backends": {
        "clang": {
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


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=repository, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _plan_html_text(declarations: dict[str, str]) -> str:
    quote = '"'
    sections = "".join(
        f"<h2 id={quote}{name}{quote}>§{name[1:]}</h2>"
        f"<p>Write paths: <code>{name}.py</code>.</p>"
        f"<p><strong>Done when</strong> the {name} check passes.</p>"
        for name in declarations
    )
    return (
        "<!doctype html>\n"
        f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>\n"
        f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>\n"
        f"<meta name={quote}plan-slug{quote} content={quote}{PLAN}{quote}>\n"
        f"<meta name={quote}plan-title{quote} content={quote}Alpha{quote}>\n"
        f"<meta name={quote}plan-status{quote} content={quote}active{quote}>\n"
        f"<meta name={quote}plan-section-declarations{quote} content={quote}"
        f"{json.dumps(declarations).replace(quote, '&quot;')}{quote}>\n"
        f"{sections}\n"
    )


def _inventory(declarations: dict[str, str]) -> list[dict]:
    return [
        {
            "slug": PLAN,
            "title": "Alpha",
            "type": "plan",
            "status": "active",
            "impl": 0.0,
            "depends_on": [],
            "effort": "M",
            "roi": "high",
            "blocking": [],
            "gates": [],
            "followups": [],
            "section_declarations": dict(declarations),
            "sections": [],
        }
    ]


def _seed(repository: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout with one plan, one crew home, and a ledger that may be empty."""

    config_home = repository.parent / "config"
    config_home.mkdir(exist_ok=True)
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    docs = repository / "docs"
    (docs / "plans").mkdir(parents=True, exist_ok=True)
    (docs / "state" / PROJECT).mkdir(parents=True, exist_ok=True)
    (docs / "state" / PROJECT / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(docs)}), encoding="utf-8"
    )
    return docs


def _promoted_row(
    repository: Path,
    *,
    plan: str,
    section: str,
    node: str,
    head: str,
    gate: str = "passed",
):
    """One promoted ledger row, which is what a delivered run leaves behind."""

    path = ledger.ledger_path(PROJECT, repository)
    if not path.exists():
        ledger.write(
            PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=repository
        )
    record = ledger.build_record(
        run_id=f"run-{node}",
        plan=plan,
        section=section,
        node=node,
        role="implement",
        gate=gate,
        commits=[head],
        completed_at="2026-11-10T10:00:00Z",
    )
    ledger.append_run(PROJECT, record, root=repository)


def _awaiting_pointer(*, section: str, node: str) -> dict:
    return {
        "run_id": f"run-{node}",
        "project": PROJECT,
        "member": "worker-two",
        "phase": "awaiting-upstream",
        "created_at": "2026-11-12T09:00:00Z",
        "node": {"id": node, "plan": PLAN, "section": section, "role": "implement"},
    }


def _section_row(report: dict, ref: str) -> dict | None:
    for row in report["immediate_roadmap"]:
        if row.get("ref") == ref:
            return row
    return None


@pytest.fixture()
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repository = tmp_path / "checkout"
    repository.mkdir()
    _seed(repository, monkeypatch)
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repository, "add", "--", "seed.txt")
    _git(repository, "commit", "-q", "-m", "chore: seed")
    (repository / "docs" / "plans" / f"{PLAN}.html").write_text(
        _plan_html_text({"s1": "implementable", "s2": "implementable"}),
        encoding="utf-8",
    )
    return repository


def test_a_delivered_section_is_not_ready_and_a_second_node_keeps_its_section(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = _git(checkout, "rev-parse", "HEAD")
    _promoted_row(checkout, plan=PLAN, section="s1", node="n-one", head=head)
    # The second section's node exists as a stored request that is not holding:
    # it waits on work it declared, so that section still has work to dispatch.
    monkeypatch.setattr(
        "reckon.crew.list_live",
        lambda: [_awaiting_pointer(section="s2", node="n-two")],
    )

    report = roadmap.build_roadmap(
        PROJECT,
        _inventory({"s1": "implementable", "s2": "implementable"}),
        [],
        docs_dir=checkout / "docs",
    )
    row = next(item for item in report["pending_work"] if item["slug"] == PLAN)

    assert "s1" not in row["ready_sections"]
    assert "s2" in row["ready_sections"]
    verdict = row["section_verdicts"]["s1"]
    assert verdict["ready"] is False
    holds = [blocker for blocker in verdict["blockers"] if blocker["kind"] == "run"]
    assert [hold["run_id"] for hold in holds] == ["run-n-one"]
    assert holds[0]["state"] == "promoted"

    # The section row names the run that holds it, so a reader is not left with
    # a section that has gone quiet and no reason.
    held_row = _section_row(report, f"{PLAN}#s1")
    assert held_row is not None
    assert held_row["ready"] is False
    assert "run-n-one" in [hold.get("run_id") for hold in held_row["blockers"]]


def test_a_live_delivered_run_holds_its_node(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that has finished and not yet been promoted holds its own node."""

    delivered = {
        "run_id": "run-n-live",
        "project": PROJECT,
        "member": "worker-three",
        "phase": "complete",
    }
    delivered["node"] = {"id": "n-live", "plan": PLAN, "section": "s1"}
    monkeypatch.setattr("reckon.crew.list_live", lambda: [delivered])

    report = roadmap.build_roadmap(
        PROJECT,
        _inventory({"s1": "implementable"}),
        [],
        docs_dir=checkout / "docs",
    )
    row = next(item for item in report["pending_work"] if item["slug"] == PLAN)

    assert "s1" not in row["ready_sections"]
    assert [hold["state"] for hold in row["section_verdicts"]["s1"]["blockers"]] == [
        "live"
    ]


@pytest.mark.parametrize("gate", ledger.GATE_VERDICTS)
def test_a_delivery_promoted_under_any_gate_holds_its_node(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, gate: str
) -> None:
    """A row's gate records the run's own check, not whether it delivered.

    Every row the picker projection returns is a completed promotion. The run's
    gate may be ``passed``, ``not-run`` (the check's evidence could not be
    produced) or ``failed`` (the check lost, and the promotion was still taken),
    and all three delivered the node they name.
    """

    head = _git(checkout, "rev-parse", "HEAD")
    _promoted_row(checkout, plan=PLAN, section="s1", node="n-one", head=head, gate=gate)
    monkeypatch.setattr("reckon.crew.list_live", list)

    report = roadmap.build_roadmap(
        PROJECT, _inventory({"s1": "implementable"}), [], docs_dir=checkout / "docs"
    )
    row = next(item for item in report["pending_work"] if item["slug"] == PLAN)

    assert "s1" not in row["ready_sections"]
    holds = row["section_verdicts"]["s1"]["blockers"]
    assert [(hold["state"], hold["run_id"]) for hold in holds] == [
        ("promoted", "run-n-one")
    ]


def test_a_released_pointer_never_frees_a_node_a_row_delivered(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead pointer for a delivered node must not put the section back in play.

    The section's node was delivered, and a later attempt on it stopped without
    delivering. The pointer names the node as free; the promoted row says that
    node was built, and the row is the newer record of the two. A section whose
    only pointer is dead, with no row behind it, stays ready — the fix must not
    read every stopped run as delivered.
    """

    head = _git(checkout, "rev-parse", "HEAD")
    _promoted_row(checkout, plan=PLAN, section="s1", node="n-one", head=head)
    monkeypatch.setattr(
        "reckon.crew.list_live",
        lambda: [
            {
                "run_id": "run-n-retry",
                "project": PROJECT,
                "member": "worker-four",
                "phase": "stopped",
                "node": {"id": "n-one", "plan": PLAN, "section": "s1"},
            },
            {
                "run_id": "run-n-abandoned",
                "project": PROJECT,
                "member": "worker-five",
                "phase": "stopped",
                "node": {"id": "n-two", "plan": PLAN, "section": "s2"},
            },
        ],
    )

    report = roadmap.build_roadmap(
        PROJECT,
        _inventory({"s1": "implementable", "s2": "implementable"}),
        [],
        docs_dir=checkout / "docs",
    )
    row = next(item for item in report["pending_work"] if item["slug"] == PLAN)

    assert "s1" not in row["ready_sections"]
    assert [hold["run_id"] for hold in row["section_verdicts"]["s1"]["blockers"]] == [
        "run-n-one"
    ]
    assert "s2" in row["ready_sections"]


def _batch() -> list[dict]:
    return [
        {
            "project": PROJECT,
            "plan": PLAN,
            "section": "s1",
            "node": "n-one",
            "goal": "build the delivered section again",
            "time_budget": "20m",
            "spec_level": "exact",
        },
        {
            "project": PROJECT,
            "plan": PLAN,
            "section": "s1",
            "node": "n-other",
            "goal": "build the section's second node",
            "time_budget": "20m",
            "spec_level": "exact",
        },
    ]


def test_a_batch_entry_for_a_delivered_node_is_answered_held_while_a_sibling_queues(
    checkout: Path,
) -> None:
    head = _git(checkout, "rev-parse", "HEAD")
    _promoted_row(checkout, plan=PLAN, section="s1", node="n-one", head=head)

    result = dispatch_batch.queue_batch(
        _batch(),
        session="coordinator-a",
        config_for=lambda _project: json.loads(json.dumps(CONFIG)),
        repo_root=checkout,
    )
    rows = {row["node"]: row for row in result["results"]}

    assert rows["n-one"]["status"] == "held"
    assert [hold["run_id"] for hold in rows["n-one"]["held_by"]] == ["run-n-one"]
    assert rows["n-other"]["status"] in {"queued", "awaiting-upstream"}
    assert result["held"] == 1
    assert result["queued"] == 1
    # Only the sibling stored a request: the held entry was answered, not queued.
    stored = {
        str((pointer.get("node") or {}).get("id")) for pointer in runs.list_live()
    }
    assert stored == {"n-other"}


def test_a_done_section_whose_run_never_landed_is_a_finding(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (checkout / "docs" / "plans" / f"{PLAN}.html").write_text(
        _plan_html_text({"s1": "done"}), encoding="utf-8"
    )
    # A head the integration branch cannot reach: the run recorded work that
    # shipped nowhere.
    _git(checkout, "checkout", "-q", "-b", "side")
    (checkout / "side.txt").write_text("side\n", encoding="utf-8")
    _git(checkout, "add", "--", "side.txt")
    _git(checkout, "commit", "-q", "-m", "feat: side work")
    side_head = _git(checkout, "rev-parse", "HEAD")
    _git(checkout, "checkout", "-q", "main")
    _promoted_row(checkout, plan=PLAN, section="s1", node="n-one", head=side_head)
    monkeypatch.setattr("reckon.crew.list_live", list)

    report = roadmap.build_roadmap(
        PROJECT, _inventory({"s1": "done"}), [], docs_dir=checkout / "docs"
    )
    found = [
        finding
        for finding in report["wiring_findings"]
        if finding.get("code") == "run-not-landed"
    ]

    assert len(found) == 1
    assert found[0]["severity"] == "warn"
    assert found[0]["extra"]["run_id"] == "run-n-one"
    assert found[0]["extra"]["head"] == side_head
    assert PLAN in found[0]["message"] and "run-n-one" in found[0]["message"]


def test_a_done_section_whose_run_landed_emits_no_finding(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction: the same fixture with the head on the branch."""

    (checkout / "docs" / "plans" / f"{PLAN}.html").write_text(
        _plan_html_text({"s1": "done"}), encoding="utf-8"
    )
    head = _git(checkout, "rev-parse", "HEAD")
    _promoted_row(checkout, plan=PLAN, section="s1", node="n-one", head=head)
    monkeypatch.setattr("reckon.crew.list_live", list)

    report = roadmap.build_roadmap(
        PROJECT, _inventory({"s1": "done"}), [], docs_dir=checkout / "docs"
    )

    assert [
        finding
        for finding in report["wiring_findings"]
        if finding.get("code") == "run-not-landed"
    ] == []
