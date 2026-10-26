"""The drain names implementable plan sections with no request or landing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import queue_admission, runs
from reckon.crew.dispatch_admission import LaneHeld
from tests import test_a_queued_request_waits_on_its_dependencies as fixtures

chain_repo = fixtures.chain_repo


def test_drain_names_implementable_sections_without_requests(
    chain_repo: Path,
) -> None:
    assert runs.drain(fixtures.PROJECT)["unloaded"] == {
        fixtures.PLAN: ["s1", "s2", "s3"]
    }


def _write_two_chains(repository: Path) -> None:
    declarations = {f"s{number}": "implementable" for number in range(1, 7)}
    waits = {
        "s2": [f"{fixtures.PLAN}#s1"],
        "s3": [f"{fixtures.PLAN}#s2"],
        "s5": [f"{fixtures.PLAN}#s4"],
    }
    path = repository / "docs" / "plans" / f"{fixtures.PLAN}.html"
    path.write_text(
        "<!doctype html>\n"
        f'<meta name="docs-project" content="{fixtures.PROJECT}">\n'
        '<meta name="reckon-type" content="plan">\n'
        f'<meta name="plan-slug" content="{fixtures.PLAN}">\n'
        '<meta name="plan-status" content="active">\n'
        '<meta name="plan-section-declarations" content="'
        f'{json.dumps(declarations).replace(chr(34), "&quot;")}">\n'
        '<meta name="plan-section-depends-on" content="'
        f'{json.dumps(waits).replace(chr(34), "&quot;")}">\n'
        + "".join(
            f'<h2 id="s{number}">Section {number}</h2>\n' for number in range(1, 7)
        ),
        encoding="utf-8",
    )
    fixtures._run(repository, "add", "--", str(path.relative_to(repository)))
    fixtures._run(
        repository,
        "commit",
        "-q",
        "-m",
        "chore: declare sections",
        "-m",
        "Add two independent chains and one section without a request.",
    )


def _dispatch(repository: Path, node: crew.TaskNode, config: dict, run_id: str) -> dict:
    try:
        return crew.dispatch(
            node=node,
            project=fixtures.PROJECT,
            repo=repository,
            config=config,
            session=fixtures.SESSION,
            run_id=run_id,
            check_budget=False,
            watch_override=True,
            unreviewed_plan_override=True,
            local=True,
            launcher=lambda *_args, **_kwargs: pytest.fail("the held lane launched"),
        )
    except LaneHeld as held:
        return dict(held.queued or {})


def test_whole_width_stores_once_and_releases_in_dependency_order(
    chain_repo: Path,
) -> None:
    _write_two_chains(chain_repo)
    lane = chain_repo.parent / "lane.json"
    lane.write_text(json.dumps({"headroom": 0, "admission": {"worker_slots": 0}}))
    config = json.loads(json.dumps(fixtures.CONFIG))
    config["backends"]["clive"]["lane_document"] = str(lane)

    nodes = [fixtures._node(f"node-{number}", f"s{number}") for number in range(1, 7)]
    stored = {
        node.section: _dispatch(chain_repo, node, config, f"r-{node.id}")
        for node in nodes[:5]
    }
    assert len(runs.list_live(project=fixtures.PROJECT)) == 5
    assert {section: row["phase"] for section, row in stored.items()} == {
        "s1": "queued",
        "s2": "awaiting-upstream",
        "s3": "awaiting-upstream",
        "s4": "queued",
        "s5": "awaiting-upstream",
    }

    pointers_before = {path.name for path in runs.live_dir().glob("*.json")}
    assert runs.drain(fixtures.PROJECT)["unloaded"] == {fixtures.PLAN: ["s6"]}
    assert {path.name for path in runs.live_dir().glob("*.json")} == pointers_before

    again = _dispatch(chain_repo, nodes[1], config, "r-repeated")
    assert again["run_id"] == stored["s2"]["run_id"]
    assert {path.name for path in runs.live_dir().glob("*.json")} == pointers_before

    _dispatch(chain_repo, nodes[5], config, "r-sixth")
    assert runs.drain(fixtures.PROJECT)["unloaded"] == {}
    assert len(runs.list_live(project=fixtures.PROJECT)) == 6

    lane.write_text(json.dumps({"headroom": 6, "admission": {"worker_slots": 6}}))
    launched: list[str] = []

    def admit() -> None:
        queue_admission.admit_once(
            config_for=lambda _pointer: config,
            launcher=fixtures._launcher(launched),
        )

    def phase(number: int) -> str:
        return str(runs.read_pointer(f"r-node-{number}").get("phase") or "")

    admit()
    assert {phase(number) for number in (2, 3, 5)} == {"awaiting-upstream"}
    assert len(launched) == 3
    fixtures._declare(chain_repo, "s1")
    admit()
    assert phase(2) != "awaiting-upstream"
    assert phase(3) == "awaiting-upstream"
    assert len(launched) == 4
    fixtures._declare(chain_repo, "s4")
    admit()
    assert phase(5) != "awaiting-upstream"
    assert len(launched) == 5
    fixtures._declare(chain_repo, "s2")
    admit()
    assert phase(3) != "awaiting-upstream"
    assert len(launched) == 6

    row = ledger.run_path(fixtures.PROJECT, "r-landed")
    row.parent.mkdir(parents=True, exist_ok=True)
    row.write_text(
        json.dumps({"run_id": "r-landed", "plan": fixtures.PLAN, "section": "s6"})
    )
    runs.pointer_path("r-sixth").unlink()
    assert runs.drain(fixtures.PROJECT)["unloaded"] == {}
