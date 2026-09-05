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
    cache_read_input_tokens: int | None = None,
    cache_creation_input_tokens: int | None = None,
    coordinator_input_tokens: int | None = 10,
    attempt: int = 1,
    changed_lines: int = 10,
) -> dict[str, Any]:
    manifest_path = (
        repository.parent / "config" / "crew" / "runs" / run_id / "manifest.md"
    )
    tokens = {"input_tokens": input_tokens}
    if cache_read_input_tokens is not None:
        tokens["cache_read_input_tokens"] = cache_read_input_tokens
    if cache_creation_input_tokens is not None:
        tokens["cache_creation_input_tokens"] = cache_creation_input_tokens
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
        budget={"tokens": tokens},
        changed_lines={"added": changed_lines, "removed": 0, "files": 1},
        manifest_path=str(manifest_path),
    )
    record["tool_steps"] = tool_steps
    record["attempt"] = attempt
    record["attempt_kind"] = "redispatch" if attempt > 1 else "initial"
    ledger.append_run(repository.name, record, root=repository)
    return record


def _write_orientation_stream(record: dict[str, Any]) -> Path:
    stream = Path(record["manifest_path"]).parent / "stream.jsonl"
    stream.parent.mkdir(parents=True)
    events = [
        {
            "type": "assistant",
            "message": {
                "id": "inspect",
                "content": [{"type": "text", "text": "reading"}],
                "usage": {
                    "input_tokens": 2,
                    "cache_creation_input_tokens": 30,
                    "cache_read_input_tokens": 68,
                },
            },
        },
        {
            "type": "assistant",
            "message": {
                "id": "inspect",
                "content": [{"type": "text", "text": "reading"}],
                "usage": {
                    "input_tokens": 2,
                    "cache_creation_input_tokens": 30,
                    "cache_read_input_tokens": 68,
                },
            },
        },
        {
            "type": "assistant",
            "message": {
                "id": "first-write",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Write",
                        "input": {"file_path": "src/shared.py"},
                    }
                ],
                "usage": {
                    "input_tokens": 2,
                    "cache_creation_input_tokens": 20,
                    "cache_read_input_tokens": 98,
                },
            },
        },
        {
            "type": "assistant",
            "message": {
                "id": "after-write",
                "content": [{"type": "text", "text": "testing"}],
                "usage": {
                    "input_tokens": 2,
                    "cache_creation_input_tokens": 10,
                    "cache_read_input_tokens": 150,
                },
            },
        },
    ]
    stream.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    return stream


@pytest.fixture()
def routing_ledgers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    mounts: dict[str, str] = {}
    repositories = {
        name: _project(tmp_path, name, mounts) for name in ("alpha", "beta")
    }

    first = _run(
        repositories["alpha"],
        "first",
        path="src/shared.py",
        tool_steps=2,
        input_tokens=2,
        cache_read_input_tokens=98,
        coordinator_input_tokens=10,
        changed_lines=10,
    )
    orientation_stream = _write_orientation_stream(first)
    _run(
        repositories["alpha"],
        "second",
        path="src/shared.py",
        gate="failed",
        tool_steps=4,
        input_tokens=2,
        cache_creation_input_tokens=20,
        cache_read_input_tokens=178,
        coordinator_input_tokens=20,
        attempt=2,
        changed_lines=30,
    )
    _run(
        repositories["beta"],
        "third",
        path="src/independent.py",
        tool_steps=8,
        input_tokens=2,
        cache_creation_input_tokens=20,
        cache_read_input_tokens=278,
        coordinator_input_tokens=30,
        changed_lines=50,
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
        "orientation_stream": orientation_stream,
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


def test_routing_reports_orientation_floor_from_regression_and_its_stream(
    routing_ledgers: dict[str, Any],
) -> None:
    report = capabilities.derive_routing(routing_ledgers["mounts"])
    row = _primary_row(report["rows"])
    floor = row["orientation_floor"]

    assert floor["status"] == "measured"
    assert floor["grouped_by"] == ["model", "role", "spec_level"]
    assert floor["samples"] == 4
    assert floor["intercept_input_tokens"] == pytest.approx(50)
    assert floor["tokens_per_changed_line"] == pytest.approx(5)

    events = [
        json.loads(line)
        for line in routing_ledgers["orientation_stream"].read_text().splitlines()
    ]
    unique_messages: dict[str, dict[str, Any]] = {}
    for event in events:
        message = event["message"]
        unique_messages.setdefault(message["id"], message)
        if any(
            block.get("type") == "tool_use" and block.get("name") == "Write"
            for block in message["content"]
        ):
            break
    stream_total = sum(
        sum(
            message["usage"].get(name, 0)
            for name in (
                "input_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            )
        )
        for message in unique_messages.values()
    )
    assert floor["stream_samples"] == 1
    assert floor["median_stream_input_tokens_before_first_write"] == stream_total

    medium_row = next(
        item
        for item in report["rows"]
        if (
            item["model"],
            item["effort"],
            item["spec_level"],
            item["role"],
        )
        == ("worker-model", "medium", "guided", "implement")
    )
    assert medium_row["orientation_floor"] == floor


def test_routing_reports_an_unobserved_orientation_floor_as_unknown(
    routing_ledgers: dict[str, Any],
) -> None:
    report = capabilities.derive_routing(routing_ledgers["mounts"])
    row = next(item for item in report["rows"] if item["model"] == "alternate-model")
    floor = row["orientation_floor"]

    assert floor["samples"] == 1
    assert floor["status"] == "unknown"
    assert floor["intercept_input_tokens"] is None
    assert floor["tokens_per_changed_line"] is None


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


def test_routing_never_ranks_an_unmetered_lane_ahead_of_a_metered_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the routing surface reporting an invented dollar figure.

    Measured on this repository's own ledger: clive, an unmetered backend
    wired against hardware this operation owns, recorded a median $21.59 per
    node against claude's $1.61 - the free lane sorted as roughly thirteen
    times the metered one's cost. Those two figures are this test's fixture
    values, not synthesised ones.
    """
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    mounts: dict[str, str] = {}
    repository = _project(tmp_path, "gamma", mounts)

    def _cost_run(run_id: str, *, backend: str, model: str, cost_usd: float) -> None:
        record = ledger.build_record(
            run_id=run_id,
            plan="delivery",
            gate="passed",
            node_definition={"id": run_id, "write_paths": [f"src/{run_id}.py"]},
            role="implement",
            spec_level="guided",
            backend=backend,
            agent={"model": model, "effort": "high"},
            completed_at_source="provided",
            budget={"cost_usd": cost_usd, "cost_usd_cumulative": cost_usd},
        )
        ledger.append_run(repository.name, record, root=repository)

    _cost_run("clive-a", backend="clive", model="deepseek-v4-flash", cost_usd=21.59)
    _cost_run("clive-b", backend="clive", model="deepseek-v4-flash", cost_usd=19.28)
    _cost_run("claude-a", backend="claude", model="worker-model", cost_usd=1.61)
    _cost_run("claude-b", backend="claude", model="worker-model", cost_usd=1.5)

    report = capabilities.derive_routing(mounts)
    rows = {row["model"]: row for row in report["rows"]}
    free_row = rows["deepseek-v4-flash"]
    metered_row = rows["worker-model"]

    # The free lane's cost is omitted from ranking and its suppression named,
    # never a silent dollar figure.
    assert free_row["median_cost_usd"]["value"] is None
    assert free_row["median_cost_usd"]["cost_usd_samples"] == 0
    assert free_row["median_cost_usd"]["cost_usd_imputed_samples"] == 2
    assert free_row["median_cost_usd"]["cost_usd_imputed"] is True

    # The metered lane's own recorded cost is unchanged by the fix.
    assert metered_row["median_cost_usd"]["value"] == pytest.approx(1.555)
    assert metered_row["median_cost_usd"]["cost_usd_samples"] == 2
    assert metered_row["median_cost_usd"]["cost_usd_imputed_samples"] == 0
    assert metered_row["median_cost_usd"]["cost_usd_imputed"] is False

    # A consumer sorting on the reported value alone no longer ranks the
    # unmetered lane as costlier than the metered one - it sorts last, as
    # unknown, rather than first as an invented $21.59.
    ranked = sorted(
        report["rows"],
        key=lambda row: (
            row["median_cost_usd"]["value"] is None,
            row["median_cost_usd"]["value"] or 0.0,
        ),
    )
    assert ranked[0]["model"] == "worker-model"
    assert ranked[-1]["model"] == "deepseek-v4-flash"


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
