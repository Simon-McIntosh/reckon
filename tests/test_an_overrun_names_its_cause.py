"""An overrun names its measured cause without turning a soft limit into a signal."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from reckon import ledger
from reckon.crew import (
    recovery,
    recovery_classification,
    recovery_liveness,
    recovery_review_delivery,
    recovery_stream,
    recovery_watch,
)
from reckon.crew.query import project_live_rows


def _run(*, tokens: int = 41_434, rate: float | None = 11.53) -> dict:
    started = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    throughput = {"generated_tokens": tokens}
    if rate is not None:
        throughput["tokens_per_second"] = rate
    return {
        "run_id": "rate-specimen",
        "project": "sample",
        "backend": "clive",
        "agent": {"model": "deepseek-v4.1-flash"},
        "node": {"id": "rate-specimen", "plan": "sample", "time_budget": "1800s"},
        "created_at": started.isoformat(),
        "attempt_started_at": started.isoformat(),
        "phase": "working",
        "process_alive": True,
        "throughput": throughput,
    }


def _cause(record: dict, monkeypatch, *, reference: float | None = 49.0) -> dict:
    monkeypatch.setattr(
        recovery_stream, "_historical_reference_rate", lambda *args: (reference, 12)
    )
    started = datetime.fromisoformat(record["created_at"])
    now = (started + timedelta(seconds=3600)).timestamp()
    timing = recovery._budget_timing(record, now_seconds=now)
    return recovery._budget_overrun_cause(record, timing, now_seconds=now)


def test_slow_measured_run_is_lane_saturated_not_over_large(monkeypatch) -> None:
    record = _run()
    cause = _cause(record, monkeypatch)

    assert 41_434 / 49.0 < 1800
    assert cause["budget_overrun_cause"] == "lane-saturated"
    assert cause["budget_overrun_rate"] == 11.53
    assert cause["budget_overrun_reference_rate"] == 49.0


def test_normal_rate_run_over_budget_is_over_large(monkeypatch) -> None:
    record = _run(tokens=100_000, rate=49.0)
    assert _cause(record, monkeypatch)["budget_overrun_cause"] == "over-large"


def test_missing_run_rate_or_reference_is_unknown(monkeypatch) -> None:
    assert _cause(_run(rate=None), monkeypatch)["budget_overrun_cause"] == "unknown"
    assert (
        _cause(_run(), monkeypatch, reference=None)["budget_overrun_cause"] == "unknown"
    )


def test_live_view_carries_the_cause(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew-home"))
    monkeypatch.setattr(
        recovery_stream, "_historical_reference_rate", lambda *args: (49.0, 12)
    )
    record = _run()
    record["manifest_path"] = str(tmp_path / "absent-manifest.md")
    record["log_path"] = str(tmp_path / "absent-stream.jsonl")
    started = datetime.fromisoformat(record["created_at"])
    observation_time = (started + timedelta(seconds=3600)).timestamp()
    (monkeypatch.setattr(recovery_stream, "_utc_seconds", lambda: observation_time), monkeypatch.setattr(recovery_liveness, "_utc_seconds", lambda: observation_time), monkeypatch.setattr(recovery_classification, "_utc_seconds", lambda: observation_time), monkeypatch.setattr(recovery_watch, "_utc_seconds", lambda: observation_time))

    row = project_live_rows([record], fields=["budget_overrun_cause"])[0]

    assert row["run_id"] == "rate-specimen"
    assert row["budget_overrun_cause"] == "lane-saturated"


def test_reference_reads_recent_committed_rows_for_one_backend_and_model(
    monkeypatch,
) -> None:
    completed = "2026-10-03T11:00:00Z"
    rows = [
        {
            "backend": "clive",
            "agent": {"model": "deepseek-v4.1-flash"},
            "completed_at": completed,
            "throughput": {"tokens_per_second": rate},
        }
        for rate in range(40, 50)
    ]
    rows.append(
        {**rows[0], "backend": "other", "throughput": {"tokens_per_second": 900}}
    )
    monkeypatch.setattr(ledger, "load", lambda project: ({"runs": rows}, 1))
    recovery._historical_reference_rate.cache_clear()
    bucket = int(datetime(2026, 10, 3, 12, tzinfo=UTC).timestamp() // 300)

    assert recovery._historical_reference_rate(
        "sample", "clive", "deepseek-v4.1-flash", bucket
    ) == (44.5, 10)
    rows.pop()
    rows.pop()
    recovery._historical_reference_rate.cache_clear()
    assert recovery._historical_reference_rate(
        "sample", "clive", "deepseek-v4.1-flash", bucket
    ) == (None, 9)


def _budget_signal_calls(source_root: Path) -> list[tuple[tuple[str, ...], str, int]]:
    """Walk reachable repository functions and enumerate process signal calls."""
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    imports: dict[str, dict[str, str]] = {}
    for path in source_root.rglob("*.py"):
        module = ".".join(path.relative_to(source_root.parent).with_suffix("").parts)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    imported[alias.asname or alias.name] = f"{node.module}.{alias.name}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported[alias.asname or alias.name] = alias.name
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions[f"{module}.{node.name}"] = node
        imports[module] = imported

    root = "reckon.crew.recovery_stream"
    starts = (
        f"{root}._budget_timing",
        f"{root}._token_budget_timing",
        f"{root}._budget_overrun_cause",
        f"{root}._apply_budget_watchdog",
    )
    found: list[tuple[tuple[str, ...], str, int]] = []
    seen: set[str] = set()
    pending = [(name, (name,)) for name in starts]
    while pending:
        name, chain = pending.pop()
        if name in seen or name not in functions:
            continue
        seen.add(name)
        module = name.rsplit(".", 1)[0]
        local_imports = dict(imports[module])
        for node in ast.walk(functions[name]):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    local_imports[alias.asname or alias.name] = (
                        f"{node.module}.{alias.name}"
                    )
        for node in ast.walk(functions[name]):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            if isinstance(target, ast.Name):
                called = target.id
                resolved = local_imports.get(called, f"{module}.{called}")
            elif isinstance(target, ast.Attribute):
                called = target.attr
                owner = target.value
                resolved = (
                    f"{local_imports.get(owner.id, owner.id)}.{called}"
                    if isinstance(owner, ast.Name)
                    else ""
                )
            else:
                continue
            if called in {"_signal_process_group", "terminate", "kill", "killpg"}:
                found.append((chain, called, node.lineno))
            elif resolved in functions:
                pending.append((resolved, (*chain, resolved)))
    return found


def test_budget_call_graph_has_only_the_guarded_watchdog_signal() -> None:
    root = Path(__file__).resolve().parents[1]
    signals = _budget_signal_calls(root / "reckon")
    assert [(chain, called) for chain, called, _ in signals] == [
        (("reckon.crew.recovery_stream._apply_budget_watchdog",), "_signal_process_group")
    ]

    tree = ast.parse((root / "reckon/crew/recovery_stream.py").read_text())
    watchdog = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_apply_budget_watchdog"
    )
    guards = [
        node
        for node in watchdog.body
        if isinstance(node, ast.If)
        and "enforce_budget_watchdog" in ast.unparse(node.test)
        and any(isinstance(child, ast.Return) for child in node.body)
    ]
    assert len(guards) == 1
    assert guards[0].end_lineno < signals[0][2]


def test_shipped_soft_limit_never_signals(monkeypatch) -> None:
    root = Path(__file__).resolve().parents[1]
    defaults = yaml.safe_load((root / "reckon/schema/flight-defaults.yaml").read_text())
    assert defaults["fences"]["enforce_budget_watchdog"] is False
    signalled = []
    (monkeypatch.setattr(
        recovery_review_delivery, "_signal_process_group", lambda *a, **k: signalled.append(a)
    ), monkeypatch.setattr(
        recovery_stream, "_signal_process_group", lambda *a, **k: signalled.append(a)
    ), monkeypatch.setattr(
        recovery_watch, "_signal_process_group", lambda *a, **k: signalled.append(a)
    ))
    record = _run()
    record.update({"launch": "cli", "pid": 4242})

    recovery._apply_budget_watchdog(record, defaults)

    assert record["budget_overrun"] is True
    assert signalled == []
