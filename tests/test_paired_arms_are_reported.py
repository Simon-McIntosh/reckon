"""A shadow arm is reported beside the primary it measures against.

The outcomes report a coordinator reads counts only non-shadow rows, so a
shadow and its primary could not be read as a pair there even though the
ledger holds both. The join is one function,
``reckon.capabilities.pair_shadow_arms``, which both the capability derivation
and the outcomes report call. A pair is reported with the sole control verdict,
so a confounded pair is never read as a matched one.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon import capabilities, ledger
from reckon import cli as cli_module
from reckon.crew.picker import outcomes

STAMP = "2026-10-03T04:00:00Z"

#: A shadow whose lineage substitutes only the candidate backend, its launch
#: mode and its model: the backend swap the shadow exists to measure.
CONTROLLED = {
    "substituted": {
        "backend": {"primary": "worker", "shadow": "candidate"},
        "model": {"primary": "concrete", "shadow": "candidate-model"},
    }
}

#: The same swap with the time budget varied too, so the pair also differs on
#: a dimension the shadow ladder is not measuring.
CONFOUNDED = {
    "substituted": {
        "backend": {"primary": "worker", "shadow": "candidate"},
        "time_budget": {"primary": "25m", "shadow": "60m"},
    }
}

ARM_FIELDS = {
    "run_id",
    "backend",
    "lane",
    "model",
    "route_mode",
    "gate",
    "review_total",
    "wall_seconds",
    "worker_seconds",
    "worker_seconds_source",
}


def _row(
    run_id: str,
    *,
    lane: str = "local-lane",
    model: str = "worker-model",
    route_mode: str = "shadow",
    gate: str = "passed",
    review_total: float = 80,
    wall_seconds: float = 100,
    worker_seconds: float = 90,
    worker_seconds_source: str = "stream_events",
    lineage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "run_id": run_id,
        "node": run_id,
        "plan": "sample",
        "role": "implement",
        "spec_level": "guided",
        "backend": "local",
        "lane": lane,
        "model_key": model,
        "route_mode": route_mode,
        "gate": gate,
        "outcome": "",
        "review": {"total": review_total},
        "wall_seconds": wall_seconds,
        "worker_seconds": worker_seconds,
        "worker_seconds_source": worker_seconds_source,
        "dispatched_at": STAMP,
        "completed_at": STAMP,
        "picker_selection": {"action": "route", "backend": "local", "confidence": 0.4},
    }
    if lineage is not None:
        row["lineage"] = lineage
    return row


def _shadow(run_id: str, primary: str, configuration: dict[str, Any]) -> dict[str, Any]:
    return _row(
        run_id,
        lineage={
            "kind": "shadow",
            "primary_run_id": primary,
            "configuration": configuration,
        },
    )


def _fixture_rows() -> list[dict[str, Any]]:
    return [
        _row(
            "primary-1",
            lane="primary-lane",
            model="primary-model",
            route_mode="picker",
            review_total=91,
            wall_seconds=200,
            worker_seconds=180,
            worker_seconds_source="stream_events",
        ),
        _shadow("shadow-controlled", "primary-1", CONTROLLED),
        _shadow("shadow-confounded", "primary-1", CONFOUNDED),
        _shadow("shadow-orphan", "absent-primary", CONTROLLED),
    ]


def _paired_entries(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["shadow"]["run_id"]: entry for entry in report["paired_arms"]}


def _function_source(module: Any, name: str) -> str:
    source = Path(module.__file__).read_text()
    tree = ast.parse(source)
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    return ast.get_source_segment(source, functions[name]) or ""


def test_paired_arms_report_the_control_verdict_and_both_arms() -> None:
    report = outcomes.summarize({"demo": _fixture_rows()}, {})
    entries = _paired_entries(report)

    assert set(entries) == {"shadow-controlled", "shadow-confounded"}
    assert entries["shadow-controlled"]["controlled"] is True
    assert entries["shadow-confounded"]["controlled"] is False
    for entry in entries.values():
        assert set(entry["shadow"]) == ARM_FIELDS
        assert set(entry["primary"]) == ARM_FIELDS

    controlled = entries["shadow-controlled"]
    assert controlled["primary"] == entries["shadow-confounded"]["primary"]
    assert controlled["primary"] == {
        "run_id": "primary-1",
        "backend": "local",
        "lane": "primary-lane",
        "model": "primary-model",
        "route_mode": "picker",
        "gate": "passed",
        "review_total": 91,
        "wall_seconds": 200,
        "worker_seconds": 180,
        "worker_seconds_source": "stream_events",
    }
    assert controlled["shadow"] == {
        "run_id": "shadow-controlled",
        "backend": "local",
        "lane": "local-lane",
        "model": "worker-model",
        "route_mode": "shadow",
        "gate": "passed",
        "review_total": 80,
        "wall_seconds": 100,
        "worker_seconds": 90,
        "worker_seconds_source": "stream_events",
    }


def test_a_shadow_without_its_primary_is_listed_as_unpaired() -> None:
    report = outcomes.summarize({"demo": _fixture_rows()}, {})
    unpaired = {entry["shadow"]["run_id"]: entry for entry in report["unpaired"]}

    assert set(unpaired) == {"shadow-orphan"}
    assert unpaired["shadow-orphan"]["primary_run_id"] == "absent-primary"
    assert unpaired["shadow-orphan"]["shadow"]["lane"] == "local-lane"
    assert unpaired["shadow-orphan"]["project"] == "demo"


def test_shadow_rows_still_do_not_enter_the_grouped_outcome_counts() -> None:
    alone = outcomes.summarize({"demo": [_fixture_rows()[0]]}, {})
    with_shadows = outcomes.summarize({"demo": _fixture_rows()}, {})

    assert with_shadows["routed_outcomes"] == alone["routed_outcomes"]
    assert with_shadows["approximate_outcomes"] == alone["approximate_outcomes"]
    assert with_shadows["mechanics"]["route_modes"]["shadow"] == 3


def test_cli_outcomes_carries_the_paired_arms_block(monkeypatch) -> None:
    expected = outcomes.summarize({"demo": _fixture_rows()}, {})
    monkeypatch.setattr(outcomes, "read_outcomes", lambda **kwargs: expected)
    result = CliRunner().invoke(
        cli_module.main, ["crew", "pick", "--outcomes", "--project", "demo"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["paired_arms"] == expected["paired_arms"]
    assert [entry["shadow"]["run_id"] for entry in payload["paired_arms"]] == [
        "shadow-controlled",
        "shadow-confounded",
    ]


def test_the_join_is_one_function_both_readers_call() -> None:
    assert callable(capabilities.pair_shadow_arms)

    derivation = _function_source(capabilities, "derive_capabilities")
    assert "pair_shadow_arms(" in derivation
    # The primary lookup lives only in the join: the derivation reads the pair
    # it is handed rather than indexing the ledger's runs itself.
    assert "runs_by_id" not in derivation
    assert "runs_by_id.get" in _function_source(capabilities, "pair_shadow_arms")
    assert "pair_shadow_arms(" in _function_source(outcomes, "summarize")


@pytest.fixture()
def home(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "config"
    root.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(root))
    return root


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "state" / "project").mkdir(parents=True)
    ledger.write("project", {"members": [], "runs": [], "holds": []}, 0, root=root)
    (root / "docs" / "plans" / "work.html").write_text(
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="project">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="work">'
        '<meta name="plan-effort-hours" content="2.0">'
        "<title>work</title></head><body></body></html>"
    )
    return root


def _append(
    root: Path, run_id: str, *, gate: str, lineage: dict[str, Any] | None
) -> None:
    record = ledger.build_record(
        run_id=run_id,
        plan="work",
        gate=gate,
        agent={"backend": "worker", "model": "concrete", "effort": "high"},
        worker_seconds=3600,
        completed_at_source="stream_mtime",
        spec_level="guided",
        lineage=lineage,
    )
    ledger.append_run("project", record, root=root)


def test_derive_capabilities_shadow_observations_are_unchanged(home, tmp_path) -> None:
    root = _project(tmp_path)
    _append(root, "primary", gate="passed", lineage=None)
    for run_id, gate, configuration in (
        ("shadow-controlled", "failed", CONTROLLED),
        ("shadow-confounded", "passed", CONFOUNDED),
        ("shadow-orphan", "passed", CONTROLLED),
    ):
        primary = "absent-primary" if run_id == "shadow-orphan" else "primary"
        _append(
            root,
            run_id,
            gate=gate,
            lineage={
                "kind": "shadow",
                "primary_run_id": primary,
                "configuration": configuration,
            },
        )

    derived = capabilities.derive_capabilities({"project": root / "docs"})
    observations = {
        entry["run_id"]: entry
        for slice_ in derived["shadow_slices"]
        for entry in slice_["observations"]
    }

    assert set(observations) == {
        "shadow-controlled",
        "shadow-confounded",
        "shadow-orphan",
    }
    assert observations["shadow-controlled"]["controlled"] is True
    assert observations["shadow-controlled"]["primary_run_id"] == "primary"
    assert observations["shadow-controlled"]["primary_gate"] == "passed"
    assert observations["shadow-controlled"]["primary_success"] is True
    assert observations["shadow-controlled"]["success"] is False
    assert observations["shadow-confounded"]["controlled"] is False
    assert observations["shadow-confounded"]["success"] is True
    assert observations["shadow-orphan"]["primary_run_id"] == "absent-primary"
    assert observations["shadow-orphan"]["primary_gate"] is None
    assert observations["shadow-orphan"]["primary_success"] is None
    # The control verdict describes the pair's configuration, not whether the
    # primary is present, so an orphan with a backend-only substitution is
    # still reported as controlled.
    assert observations["shadow-orphan"]["controlled"] is True
