"""Dispatch admission checks the filesystem where run scratch will live."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import cli, crew_dispatch_commands, flight
from reckon.crew.node import TaskNode

crew_dispatch = importlib.import_module("reckon.crew.dispatch")


def _setup(tmp_path, monkeypatch, free_blocks):
    root = tmp_path / "scratch"
    root.mkdir()
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(root))
    probes = []

    def statvfs(path):
        probes.append(Path(path))
        return SimpleNamespace(f_bavail=free_blocks, f_blocks=100, f_frsize=1024**3)

    monkeypatch.setattr(
        crew_dispatch.os,
        "statvfs",
        statvfs,
    )
    assert root != Path("/tmp/reckon-crew-scratch")  # noqa: S108 - real root comparison
    return root, probes


def test_dispatch_refuses_before_worktree_when_scratch_is_low(tmp_path, monkeypatch):
    root, probes = _setup(tmp_path, monkeypatch, 4)
    node = TaskNode(id="work", goal="work", plan="sample")
    with pytest.raises(crew_dispatch.TmpHeadroomError) as raised:
        crew_dispatch.plan_dispatch(
            node=node, config={"worktree": {}}, project="sample", repo=tmp_path
        )
    assert raised.value.free_bytes == 4 * 1024**3
    assert raised.value.floor_bytes == 10 * 1024**3
    assert "reckon crew gc --project" in str(raised.value)
    assert probes == [root]
    assert list(root.iterdir()) == []


def test_dispatch_passes_above_floor_and_flight_override_tunes_it(
    tmp_path, monkeypatch
):
    root, probes = _setup(tmp_path, monkeypatch, 12)
    assert crew_dispatch.require_worker_scratch_headroom({"worktree": {}}) == {
        "free_bytes": 12 * 1024**3,
        "floor_bytes": 10 * 1024**3,
    }
    host = tmp_path / "flight.yaml"
    host.write_text(
        f"worktree:\n  scratch_min_free_bytes: {13 * 1024**3}\n"
        "  scratch_min_free_pct: 1\n"
    )
    custom = flight.resolve(host_path=host).config
    with pytest.raises(crew_dispatch.TmpHeadroomError) as raised:
        crew_dispatch.require_worker_scratch_headroom(custom)
    assert raised.value.floor_bytes == 13 * 1024**3
    assert probes == [root, root]
    assert list(root.iterdir()) == []


def test_dry_run_reports_the_same_named_refusal(tmp_path, monkeypatch):
    root, probes = _setup(tmp_path, monkeypatch, 4)
    config = flight.resolve(host_path=tmp_path / "absent.yaml").config
    monkeypatch.setattr(crew_dispatch_commands, "_dispatch_resolved_flight", lambda *args: config)
    monkeypatch.setattr(crew_dispatch_commands, "_model_availability_refusal", lambda *args, **kw: None)
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "dispatch",
            "--project",
            "sample",
            "--plan",
            "sample",
            "--node",
            "work",
            "--session",
            "test",
            "--route",
            "deterministic",
            "--dry-run",
            "--repo",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 75, result.output
    payload = json.loads(result.output)
    assert payload["error"] == "tmp-headroom-refusal"
    assert payload["dry_run"] is True
    assert payload["free_bytes"] == 4 * 1024**3
    assert payload["floor_bytes"] == 10 * 1024**3
    assert probes and all(path == root for path in probes)
    assert list(root.iterdir()) == []
