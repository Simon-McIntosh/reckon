"""Admission continues a queued request without classifying its session backlog."""

from __future__ import annotations

import copy
import importlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import queue_admission, recovery, runs
from tests import (
    test_a_queued_launch_ignores_the_coordinator_backlog as backlog_helpers,
)
from tests.test_a_queued_launch_ignores_the_coordinator_backlog import (
    PROJECT,
    SESSION,
    _config_for,
    _launcher,
    _node,
    _queue,
    _write_overdue_run,
)

# The immutable source used for the launch compatibility comparison.
admit_repo = backlog_helpers.admit_repo
REFERENCE_REVISION = "2c04e1a4da11c68694631b3297694c611bbad839"
dispatch_module = importlib.import_module("reckon.crew.dispatch")


@pytest.fixture()
def fleet(admit_repo, monkeypatch):
    home = Path(os.environ["RECKON_HOME"])
    _write_overdue_run(home, "r-delivered-00")
    template = runs.read_pointer("r-delivered-00")
    for index in range(1, 50):
        pointer = dict(template, run_id=f"r-delivered-{index:02}")
        if index >= 10:
            pointer["session"] = "peer-session"
        runs._write_json(runs.pointer_path(pointer["run_id"]), pointer)
    assert len(runs.list_live(project=PROJECT)) == 50
    calls = []
    classify = recovery.classify_pointer

    def counted(pointer, **kwargs):
        calls.append(pointer["run_id"])
        return classify(pointer, **kwargs)

    for module in tuple(sys.modules.values()):
        if (
            getattr(module, "__name__", "").startswith("reckon.")
            and getattr(module, "classify_pointer", None) is classify
        ):
            monkeypatch.setattr(module, "classify_pointer", counted)
    return admit_repo, calls


def test_admission_classifies_only_the_request_it_launches(fleet):
    repository, calls = fleet
    _, resolve = _config_for()
    runs.queue_dispatch(_queue(repository, node_id="queued", run_id="r-queued"))
    plans = []

    result = queue_admission.admit_once(config_for=resolve, launcher=_launcher(plans))

    print("admission classifier calls:", len(calls), calls, file=sys.__stdout__)
    assert result["launched"] == ["r-queued"], result
    assert len(plans) == 1
    assert set(calls) <= {"r-queued"}, calls
    assert len(calls) <= 1, calls
    pointer = runs.read_pointer("r-queued")
    assert runs.dispatch_field(pointer, "unreconciled_override") is None
    assert pointer["admitted_from"] == "r-queued"
    assert runs.dispatch_field(pointer, "argv") == list(plans[0].argv)


def test_direct_dispatch_records_the_ten_session_runs(fleet):
    repository, calls = fleet
    config, _ = _config_for()
    record = dispatch_module.dispatch(
        node=_node("direct"),
        project=PROJECT,
        repo=repository,
        config=config,
        session=SESSION,
        launcher=_launcher([]),
        local=True,
        route="deterministic",
    )

    backlog = runs.dispatch_field(record, "unreconciled_override")
    assert backlog["source"] == "coordinator"
    assert {row["run_id"] for row in backlog["waived_runs"]} == {
        f"r-delivered-{index:02}" for index in range(10)
    }
    assert set(calls) >= {f"r-delivered-{index:02}" for index in range(50)}
    print("direct dispatch backlog:", len(backlog["waived_runs"]), file=sys.__stdout__)


def test_the_launch_inputs_equal_the_base_except_for_backlog(
    fleet, monkeypatch, tmp_path
):
    """Compare the entire launch handoff to the immutable base implementation.

    Freeze the clock and reuse the first temporary worktree for both launches
    so pointer and command differences cannot hide behind fresh identities.
    """
    repository, _ = fleet
    instant = datetime.now(UTC)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    for module in tuple(sys.modules.values()):
        if (
            getattr(module, "__name__", "").startswith("reckon.")
            and getattr(module, "datetime", None) is datetime
        ):
            monkeypatch.setattr(module, "datetime", FixedDatetime)
    source = subprocess.run(
        ["git", "show", f"{REFERENCE_REVISION}:reckon/crew/dispatching/preparation.py"],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    path = tmp_path / "reference_preparation.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location(
        "reckon.crew.dispatching.reference_preparation", path
    )
    reference = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reference)
    for name in reference._FACADE_BINDINGS:
        setattr(reference, name, getattr(dispatch_module, name))
    config, _ = _config_for()
    request = _queue(repository, node_id="queued", run_id="r-queued")
    request["dispatch_options"]["wave"] = "comparison"
    runs.queue_dispatch(request)
    original_request = copy.deepcopy(runs.read_pointer("r-queued"))
    original_detail = runs.read_dispatch_detail(original_request)

    def reset_request():
        runs.dispatch_detail_path("r-queued").write_text(json.dumps(original_detail))
        runs._write_json(runs.pointer_path("r-queued"), original_request)

    captured = []
    plans = []
    launcher = _launcher(plans)
    compose = dispatch_module._launch_dispatch

    def capture(**kwargs):
        captured.append(kwargs)
        return {"run_id": "r-queued", "phase": "starting"}

    monkeypatch.setattr(dispatch_module, "_launch_dispatch", capture)
    reference._launch_dispatch = capture
    queue_admission._launch_queued(
        request, config_for=lambda _: config, launcher=launcher
    )
    reset_request()
    monkeypatch.setattr(dispatch_module, "dispatch", reference.dispatch)
    queue_admission._launch_queued(
        request, config_for=lambda _: config, launcher=launcher
    )
    actual, expected = captured
    assert actual["backlog"] is None
    assert len(expected["backlog"]["waived_runs"]) == 10
    assert {key: value for key, value in actual.items() if key != "backlog"} == {
        key: value for key, value in expected.items() if key != "backlog"
    }
    create = dispatch_module._create_worktree
    worktrees = []

    def same_worktree(*args, **kwargs):
        if not worktrees:
            worktrees.append(create(*args, **kwargs))
        return worktrees[0]

    monkeypatch.setattr(dispatch_module, "_create_worktree", same_worktree)
    reset_request()
    head = compose(**actual)
    head_pointer = runs.read_pointer("r-queued")
    reset_request()
    base = compose(**expected)
    base_pointer = runs.read_pointer("r-queued")
    assert head.pop("unreconciled_override") is None
    assert len(base.pop("unreconciled_override")["waived_runs"]) == 10
    assert head == base
    assert head_pointer == base_pointer
    assert len(plans) == 2
    assert plans[0].argv == plans[1].argv
    print(
        "full launch records, live pointers and commands equal base except backlog",
        file=sys.__stdout__,
    )
