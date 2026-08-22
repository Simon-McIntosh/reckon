from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon import capabilities, cli as cli_module, ledger


@pytest.fixture()
def home(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "config"
    root.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(root))
    return root


def _project(tmp_path: Path, name: str = "project") -> Path:
    root = tmp_path / name
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "state" / name).mkdir(parents=True)
    return root


def _plan(root: Path, slug: str, hours: float) -> None:
    project = root.name
    (root / "docs" / "plans" / f"{slug}.html").write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{project}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        f'<meta name="plan-effort-hours" content="{hours}">'
        f"<title>{slug}</title></head><body></body></html>"
    )


def _run(
    root: Path,
    run_id: str,
    plan: str,
    actual_hours: float,
    *,
    gate: str = "passed",
    effort: str = "high",
    changed_lines: dict | None = None,
    completed_at_source: str = "stream_mtime",
    spec_level: str | None = None,
) -> None:
    build_record_kwargs: dict[str, Any] = {}
    if spec_level is not None:
        build_record_kwargs["spec_level"] = spec_level
    ledger.append_run(
        root.name,
        ledger.build_record(
            run_id=run_id,
            plan=plan,
            gate=gate,
            agent={"backend": "worker", "model": "concrete", "effort": effort},
            worker_seconds=int(actual_hours * 3600),
            completed_at_source=completed_at_source,
            changed_lines=changed_lines,
            **build_record_kwargs,
        ),
        root=root,
    )


def _derive(root: Path, **kwargs) -> dict:
    return capabilities.derive_capabilities(
        {root.name: root / "docs"},
        **kwargs,
    )


def test_cache_lives_under_reckon_config_home(home) -> None:
    assert capabilities.capabilities_path() == home / "cache" / "capabilities.json"


def test_rebuild_reads_committed_project_ledgers(home, tmp_path) -> None:
    first = _project(tmp_path, "first")
    second = _project(tmp_path, "second")
    for root in (first, second):
        _plan(root, "work", 2.0)
        _run(root, f"run-{root.name}", "work", 1.0)

    record = capabilities.rebuild_capabilities(
        mounted_docs={"first": first / "docs", "second": second / "docs"}
    )

    assert record["projects"] == ["first", "second"]
    assert sum(item["runs"] for item in record["configurations"]) == 2
    assert capabilities.capabilities_path().is_file()


def test_deleting_and_rebuilding_reproduces_identical_figures(home, tmp_path) -> None:
    root = _project(tmp_path)
    _plan(root, "work", 2.0)
    _run(root, "one", "work", 1.0)
    mounts = {root.name: root / "docs"}

    first = capabilities.rebuild_capabilities(mounted_docs=mounts)
    capabilities.capabilities_path().unlink()
    second = capabilities.rebuild_capabilities(mounted_docs=mounts)

    assert second == first


def test_agent_configuration_includes_recorded_effort_level(tmp_path) -> None:
    root = _project(tmp_path)
    _plan(root, "work", 2.0)
    _run(root, "high", "work", 1.0, effort="high")
    _run(root, "medium", "work", 1.0, effort="medium")

    configurations = _derive(root)["configurations"]

    assert len(configurations) == 2
    assert {item["configuration"]["effort"] for item in configurations} == {
        "high",
        "medium",
    }


def test_success_rate_is_conditioned_on_estimated_hours(tmp_path) -> None:
    root = _project(tmp_path)
    _plan(root, "small", 1.0)
    _plan(root, "large", 4.0)
    _run(root, "small-pass", "small", 1.0)
    _run(root, "large-fail", "large", 4.0, gate="failed")

    curve = _derive(root)["configurations"][0]["success_by_estimated_hours"]

    assert curve == [
        {"estimated_hours": 1.0, "samples": 1, "successes": 1, "success_rate": 1.0},
        {"estimated_hours": 4.0, "samples": 2, "successes": 1, "success_rate": 0.5},
    ]


def test_speed_reports_the_empirical_distribution_not_only_mean(tmp_path) -> None:
    root = _project(tmp_path)
    _plan(root, "work", 4.0)
    for index, actual in enumerate((1.0, 2.0, 4.0)):
        _run(root, str(index), "work", actual)

    speed = _derive(root)["configurations"][0]["speed"]

    assert speed["values"] == [1.0, 2.0, 4.0]
    assert speed["median"] == 2.0
    assert speed["minimum"] == 1.0
    assert speed["maximum"] == 4.0
    assert speed["p10"] < speed["p90"]


def test_competence_horizon_is_largest_size_meeting_threshold(tmp_path) -> None:
    root = _project(tmp_path)
    for slug, hours, gate in (
        ("small", 1.0, "passed"),
        ("medium", 2.0, "passed"),
        ("large", 4.0, "failed"),
    ):
        _plan(root, slug, hours)
        _run(root, slug, slug, hours, gate=gate)

    configuration = _derive(root, success_threshold=0.8)["configurations"][0]

    assert configuration["success_threshold"] == 0.8
    assert configuration["competence_horizon_hours"] == 2.0


def test_changed_lines_are_descriptive_and_never_enter_selection(tmp_path) -> None:
    root = _project(tmp_path)
    _plan(root, "work", 2.0)
    _run(
        root,
        "one",
        "work",
        1.0,
        changed_lines={"added": 8000, "removed": 3000, "files": 99},
    )
    record = _derive(root)

    observation = record["configurations"][0]["observations"][0]
    assert observation["changed_lines"] == {"added": 8000, "removed": 3000, "files": 99}
    source = Path(capabilities.__file__).read_text()
    tree = ast.parse(source)
    consumers = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and "changed_lines" in ast.get_source_segment(source, node)
    }
    assert consumers == {"_descriptive_changed_lines", "derive_capabilities"}
    derive_source = ast.get_source_segment(
        source,
        next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "derive_capabilities"
        ),
    )
    assert derive_source.count("_descriptive_changed_lines(run)") == 1


def test_spec_level_is_captured_in_observations(tmp_path) -> None:
    root = _project(tmp_path)
    _plan(root, "work", 2.0)
    _run(root, "exact", "work", 1.0, spec_level="exact")
    _run(root, "none", "work", 1.0)

    observations = _derive(root)["configurations"][0]["observations"]
    indexed = {entry["run_id"]: entry for entry in observations}
    assert indexed["exact"]["spec_level"] == "exact"
    assert indexed["none"]["spec_level"] is None


def test_cache_contains_only_directly_rederived_values(home, tmp_path) -> None:
    root = _project(tmp_path)
    _plan(root, "work", 2.0)
    _run(root, "one", "work", 1.0)
    mounts = {root.name: root / "docs"}

    cached = capabilities.rebuild_capabilities(mounted_docs=mounts)
    direct = capabilities.derive_capabilities(mounts)

    assert json.loads(capabilities.capabilities_path().read_text()) == direct == cached


def test_ledger_version_invalidates_cache_without_dispatch_rebuild(
    home, tmp_path, monkeypatch
) -> None:
    root = _project(tmp_path)
    _plan(root, "work", 2.0)
    _run(root, "one", "work", 1.0)
    mounts = {root.name: root / "docs"}
    cached = capabilities.rebuild_capabilities(mounted_docs=mounts)

    assert capabilities.inspect_capabilities(mounted_docs=mounts)["stale"] is False
    _run(root, "two", "work", 1.0)
    inspection = capabilities.inspect_capabilities(mounted_docs=mounts)

    assert inspection["stale"] is True
    assert inspection["changed_projects"] == [root.name]
    assert capabilities.project_cache_status(cached, root.name, root=root) == "stale"
    capabilities.capabilities_path().unlink()
    monkeypatch.setattr(
        capabilities,
        "rebuild_capabilities",
        lambda **kwargs: pytest.fail("cache reads must not rebuild synchronously"),
    )
    assert capabilities.load_capabilities()["cache_status"] == "missing"


def test_capabilities_cli_inspects_and_rebuilds_off_dispatch(home) -> None:
    inspected = CliRunner().invoke(cli_module.main, ["capabilities"])
    rebuilt = CliRunner().invoke(cli_module.main, ["capabilities", "--rebuild"])

    assert inspected.exit_code == rebuilt.exit_code == 0
    assert json.loads(inspected.output)["rebuilt"] is False
    payload = json.loads(rebuilt.output)
    assert payload["rebuilt"] is True
    assert payload["configurations"] == 0
    assert capabilities.capabilities_path().is_file()


def test_scope_changed_and_proxy_completion_records_are_excluded(tmp_path) -> None:
    root = _project(tmp_path)
    _plan(root, "work", 2.0)
    _run(root, "valid", "work", 1.0)
    widened = ledger.build_record(
        run_id="widened",
        plan="work",
        gate="passed",
        agent={"backend": "worker", "model": "concrete", "effort": "high"},
        worker_seconds=3600,
        completed_at_source="stream_mtime",
        scope_changed=True,
    )
    proxy = {
        **widened,
        "run_id": "proxy",
        "scope_changed": False,
        "completed_at_source": "promotion_time",
    }
    ledger.append_run(root.name, widened, root=root)
    ledger.append_run(root.name, proxy, root=root)

    record = _derive(root)

    assert record["configurations"][0]["runs"] == 1
    assert record["excluded"] == {
        "scope_changed": 1,
        "stalled": 0,
        "unusable_completion": 1,
        "invalid": 0,
    }


def test_explicit_completion_time_is_usable_for_capabilities(tmp_path) -> None:
    root = _project(tmp_path)
    _plan(root, "work", 2.0)
    _run(
        root,
        "explicit",
        "work",
        1.0,
        completed_at_source="provided",
    )

    record = _derive(root)

    assert record["configurations"][0]["runs"] == 1
    assert record["excluded"]["unusable_completion"] == 0
