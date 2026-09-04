from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from reckon import capabilities, ledger, mcp, serve


def _project(root: Path, name: str, mounts: dict[str, str]) -> Path:
    repository = root / name
    (repository / "docs" / "plans").mkdir(parents=True)
    (repository / "docs" / "state" / name).mkdir(parents=True)
    mounts[name] = str(repository / "docs")
    return repository


def _coordinator(input_tokens: int | None) -> dict[str, Any]:
    return {
        "session_id": "coordinator-session",
        "authoring_turn": {
            "status": "measured" if input_tokens is not None else "unknown",
            "tokens": (
                {"input_tokens": input_tokens} if input_tokens is not None else None
            ),
        },
    }


def _run(
    repository: Path,
    run_id: str,
    *,
    plan: str = "delivery",
    path: str,
    gate: str = "passed",
    model: str = "worker-model",
    effort: str = "high",
    spec_level: str = "guided",
    role: str = "implement",
    tool_steps: int = 1,
    input_tokens: int = 100,
    coordinator_input_tokens: int | None = 10,
    attempt: int = 1,
) -> dict[str, Any]:
    record = ledger.build_record(
        run_id=run_id,
        plan=plan,
        gate=gate,
        failure_classification="work-rejected" if gate == "failed" else "",
        node_definition={
            "id": run_id,
            "write_paths": [path],
            "coordinator": _coordinator(coordinator_input_tokens),
        },
        role=role,
        spec_level=spec_level,
        agent={"model": model, "effort": effort},
        completed_at_source="provided",
        budget={"tokens": {"input_tokens": input_tokens}},
    )
    record["tool_steps"] = tool_steps
    record["attempt"] = attempt
    record["attempt_kind"] = "redispatch" if attempt > 1 else "initial"
    ledger.append_run(repository.name, record, root=repository)
    return record


@pytest.fixture()
def routing_ledgers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    mounts: dict[str, str] = {}
    repositories = {
        name: _project(tmp_path, name, mounts) for name in ("alpha", "beta")
    }

    _run(
        repositories["alpha"],
        "first",
        path="src/shared.py",
        tool_steps=2,
        input_tokens=100,
        coordinator_input_tokens=10,
    )
    _run(
        repositories["alpha"],
        "second",
        path="src/shared.py",
        gate="failed",
        tool_steps=4,
        input_tokens=200,
        coordinator_input_tokens=20,
        attempt=2,
    )
    _run(
        repositories["beta"],
        "third",
        path="src/independent.py",
        tool_steps=8,
        input_tokens=300,
        coordinator_input_tokens=30,
    )

    for index, changes in enumerate(
        (
            {"model": "alternate-model"},
            {"effort": "medium"},
            {"spec_level": "exact"},
            {"role": "review"},
        )
    ):
        _run(
            repositories["beta"],
            f"dimension-{index}",
            plan=f"dimension-{index}",
            path=f"src/dimension_{index}.py",
            **changes,
        )

    mounts_file = config_home / "mounts.json"
    mounts_file.write_text(json.dumps(mounts), encoding="utf-8")
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts_file)
    serve._DISC_CACHE.clear()
    return {
        "config_home": config_home,
        "mounts": mounts,
        "mounts_file": mounts_file,
        "repositories": repositories,
    }


def _primary_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return next(
        row
        for row in rows
        if (
            row["model"],
            row["effort"],
            row["spec_level"],
            row["role"],
        )
        == ("worker-model", "high", "guided", "implement")
    )


def test_routing_groups_every_selection_dimension_across_mounted_ledgers(
    routing_ledgers: dict[str, Any],
) -> None:
    report = capabilities.derive_routing(routing_ledgers["mounts"])
    row = _primary_row(report["rows"])

    assert report["projects"] == ["alpha", "beta"]
    assert len(report["rows"]) == 5
    assert row["samples"] == 3
    assert row["passed"] == 2
    assert row["pass_rate"] == pytest.approx(2 / 3, abs=1e-6)
    assert row["reworked"] == 1
    assert row["rework_rate"] == pytest.approx(1 / 3, abs=1e-6)
    assert row["redispatched"] == 1
    assert row["redispatch_rate"] == pytest.approx(1 / 3, abs=1e-6)
    assert row["tool_step_samples"] == 3
    assert row["median_tool_steps"] == 4
    assert row["input_samples"] == 3
    assert row["median_input_tokens"] == 200

    identities = {
        (item["model"], item["effort"], item["spec_level"], item["role"])
        for item in report["rows"]
    }
    assert ("alternate-model", "high", "guided", "implement") in identities
    assert ("worker-model", "medium", "guided", "implement") in identities
    assert ("worker-model", "high", "exact", "implement") in identities
    assert ("worker-model", "high", "guided", "review") in identities


def test_routing_charges_coordinator_spend_and_labels_back_loaded_cost(
    routing_ledgers: dict[str, Any],
) -> None:
    row = _primary_row(capabilities.derive_routing(routing_ledgers["mounts"])["rows"])

    assert row["coordinator_input_samples"] == 3
    assert row["median_coordinator_input_tokens"] == 20
    assert row["worker_plus_coordinator_samples"] == 3
    assert row["median_worker_plus_coordinator_input_tokens"] == 220

    per_run = row["per_run_cost"]
    durable = row["rework_charged_cost_per_durable_node"]
    assert per_run == {
        "label": "immediate spend; a short window can reflect this",
        "short_window_can_reflect": True,
        "worker_only_input_tokens": 200,
        "worker_plus_coordinator_input_tokens": 220,
    }
    assert durable["short_window_can_reflect"] is False
    assert "back-loaded" in durable["label"]
    assert durable["worker_only_input_tokens"] == 300
    assert durable["worker_plus_coordinator_input_tokens"] == 330
    assert (
        durable["worker_plus_coordinator_input_tokens"]
        != durable["worker_only_input_tokens"]
    )


def test_routing_counts_tool_steps_from_a_durable_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    mounts: dict[str, str] = {}
    repository = _project(tmp_path, "project", mounts)
    record = _run(repository, "streamed", path="src/result.py", tool_steps=7)
    data, version = ledger.load("project", repository)
    data["runs"][0].pop("tool_steps")
    run_directory = config_home / "crew" / "runs" / record["run_id"]
    run_directory.mkdir(parents=True)
    data["runs"][0]["manifest_path"] = str(run_directory / "manifest.md")
    ledger.write("project", data, version, repository)
    events = [
        {"type": "item.completed", "item": {"type": "reasoning"}},
        {"type": "item.completed", "item": {"type": "command_execution"}},
        {"type": "item.completed", "item": {"type": "file_change"}},
    ]
    (run_directory / "stream.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )

    row = capabilities.derive_routing(mounts)["rows"][0]

    assert row["tool_step_samples"] == 1
    assert row["median_tool_steps"] == 2


def _get(port: int, path: str) -> tuple[int, dict[str, Any]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_served_and_tool_routing_surfaces_return_identical_rows(
    routing_ledgers: dict[str, Any],
) -> None:
    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        tool = mcp._crew("alpha", view="routing")
        status, served = _get(server.server_port, "/crew/alpha/routing")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 200
    assert tool["ok"] is True
    assert served["rows"] == tool["rows"]
    assert served["ledger_versions"] == tool["ledger_versions"]


def test_capability_rebuild_carries_the_current_routing_rows(
    routing_ledgers: dict[str, Any],
) -> None:
    cached = capabilities.rebuild_capabilities(mounted_docs=routing_ledgers["mounts"])

    assert (
        cached["routing"]["rows"]
        == capabilities.derive_routing(routing_ledgers["mounts"])["rows"]
    )
