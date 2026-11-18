"""The queue reads as a queue, from the function, the CLI and the MCP tool.

One read answers where the waiting work stands: every queued and
awaiting-upstream request with the place it holds in the order admission
launches by, the reason for that place, the wait it has served and the hold
that stops it, beside each session's priority budget. The document is derived
once and served unchanged by both surfaces, so a coordinator reading the CLI
and one reading the tool cannot be told different orders for the same queue.

The requests are stored through the admission release path, the priority is set
through the queue's own reprioritise verb and the hold through its own hold
verb, so nothing here writes a shape the production writers do not.
"""

# ruff: noqa: F811 - the fixtures are imported and also named as test parameters.

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from reckon import cli
from tests.test_a_queued_request_waits_on_its_dependencies import (  # noqa: F401
    CONFIG,
    PROJECT,
    _config_for,
    _launcher,
    _node,
    chain_repo,
)

SESSION_A = "coordinator-a"
SESSION_B = "coordinator-b"


def _queue(repository, nodes, *, session, run_ids):
    """Store requests through the release path a batch dispatch uses."""
    from reckon.crew import queue_admission

    return queue_admission.queue_requests(
        nodes,
        project=PROJECT,
        session=session,
        repo_root=repository,
        config=CONFIG,
        run_ids=run_ids,
    )


@pytest.fixture()
def queue_home(chain_repo, monkeypatch):
    """The chain's checkout with a flight layer declaring a priority budget."""
    layer = chain_repo.parent / "flight.yaml"
    layer.write_text(
        json.dumps({**CONFIG, "queue": {"priority_budget": 10}}), encoding="utf-8"
    )
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(layer))
    return chain_repo


def _surfaces(project: str) -> dict[str, dict]:
    """The same queue document, read on each of its three surfaces."""
    from reckon import mcp
    from reckon.crew import queue_view as queue_view_module

    runner = CliRunner()
    result = runner.invoke(cli.main, ["crew", "view", "queue", "--project", project])
    assert result.exit_code == 0, result.output
    return {
        "function": queue_view_module.queue_view(project),
        "cli": json.loads(result.output),
        "mcp": mcp._crew(project=project, view="queue"),
    }


def _order(document: dict) -> list[tuple[str, object]]:
    return [
        (row["run_id"], row.get("position"), row["reason"])
        for row in document["requests"]
    ]


def test_the_same_order_and_reasons_come_from_every_surface(
    queue_home,
) -> None:
    from reckon.crew import queue_edit

    _queue(
        queue_home,
        [_node("a-raised", "s1"), _node("a-waits", "s3")],
        session=SESSION_A,
        run_ids={"a-raised": "r-a-raised", "a-waits": "r-a-waits"},
    )
    _queue(
        queue_home,
        [_node("b-held", "s1"), _node("b-plain", "s1")],
        session=SESSION_B,
        run_ids={"b-held": "r-b-held", "b-plain": "r-b-plain"},
    )
    queue_edit.reprioritise("r-a-raised", 3, reason="urgent")
    queue_edit.hold("r-b-held", reason="waiting for the lead's word")

    documents = _surfaces(PROJECT)

    for name, document in documents.items():
        assert document["ok"] is True, (name, document)
        assert _order(document) == _order(documents["function"]), name

    rows = {row["run_id"]: row for row in documents["function"]["requests"]}
    assert rows["r-a-raised"]["position"] == 1
    assert rows["r-a-raised"]["reason"] == "priority override 3"
    assert rows["r-a-raised"]["priority"] == 3
    assert rows["r-a-waits"]["reason"] == "waiting upstream: plan-a#s2"
    assert "position" not in rows["r-a-waits"]
    assert rows["r-a-waits"]["waits"] == ["plan-a#s2"]
    assert rows["r-b-held"]["hold"]["reason"] == "waiting for the lead's word"
    sessions = {row["session"]: row for row in documents["function"]["sessions"]}
    assert set(sessions) == {SESSION_A, SESSION_B}
    assert sessions[SESSION_A]["in_use"] == 3
    assert rows["r-a-raised"]["waited_seconds"] is not None
    print(
        f"MEASURE: order={_order(documents['function'])} surfaces={sorted(documents)}"
    )
    print(
        "MEASURE: "
        + " ".join(
            f"{name}(budget={row['budget']},in_use={row['in_use']},free={row['free']})"
            for name, row in sorted(sessions.items())
        )
    )


def test_a_pass_launches_the_request_the_view_places_first(queue_home) -> None:
    from reckon.crew import queue_admission
    from reckon.crew import queue_view as queue_view_module

    _queue(
        queue_home,
        [_node("head", "s1"), _node("middle", "s2")],
        session=SESSION_A,
        run_ids={"head": "r-head", "middle": "r-middle"},
    )

    before = queue_view_module.queue_view(PROJECT)
    first = before["requests"][0]
    assert first["position"] == 1

    launched: list[str] = []
    result = queue_admission.admit_once(
        config_for=_config_for(), launcher=_launcher(launched)
    )

    assert result["launched"] == [first["run_id"]], result
    print(
        f"MEASURE: viewed_first=({first['run_id']}, place {first['position']}, "
        f"priority {first['priority']}, reason {first['reason']!r}) "
        f"launched={result['launched']}"
    )
