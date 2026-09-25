"""Producer snapshots and emitted transitions consume one run verdict."""

from __future__ import annotations

import inspect
import os
import subprocess
import time
from pathlib import Path

import pytest

from reckon.crew import recovery, runs
from tests import test_a_live_run_never_reads_dead as liveness


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    if os.environ.get("RECKON_PRODUCER_PID_CONTROL") == "1":
        snapshot = recovery._watch_snapshot
        classifier = recovery.classify_pointer

        def own_pid_snapshot(pointer, *, moment, stall_seconds):
            row = classifier(
                pointer, now_seconds=moment, stale_after_seconds=stall_seconds
            )
            row["process_alive"] = runs.record_process_alive(pointer)
            row["fleet_verdict"] = recovery._watch_verdict(
                pointer, row, moment=moment, stall_seconds=stall_seconds
            )
            with monkeypatch.context() as control:
                control.setattr(
                    recovery, "classify_pointer", lambda *args, **kwargs: row
                )
                return snapshot(pointer, moment=moment, stall_seconds=stall_seconds)

        monkeypatch.setattr(recovery, "_watch_snapshot", own_pid_snapshot)


def _git(tree: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(tree), *args], text=True, stderr=subprocess.STDOUT
    ).strip()


def _commit(tree: Path, text: str) -> str:
    (tree / "delivery.txt").write_text(text)
    _git(tree, "add", "delivery.txt")
    _git(
        tree,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "test: record delivery\n\nKeep a durable fixture revision.",
    )
    return _git(tree, "rev-parse", "HEAD")


def test_exited_supervisor_live_worker_emits_working(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    _git(tree, "init", "-q")
    base = _commit(tree, "base\n")
    with liveness._live_child() as supervisor:
        pointer = liveness._pointer(
            tmp_path,
            "r-supervised",
            pid=supervisor,
            phase="starting",
            worktree=tree,
            base_sha=base,
            write_stream=False,
        )
        before = recovery._watch_snapshot(
            pointer, moment=time.time(), stall_seconds=3600
        )
        assert before["state"] == "dispatched"
    assert runs.process_alive(supervisor) is False

    with liveness._live_child() as worker:
        liveness._write_worker_record("r-supervised", worker)
        Path(pointer["log_path"]).write_text('{"type":"assistant"}\n')
        Path(pointer["manifest_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(pointer["manifest_path"]).write_text(
            "node: supervised\nstatus: in-progress\n"
        )
        pointer["process_alive"] = False
        _commit(tree, "delivered\n")
        assert recovery._commits_beyond_base(pointer) == 1
        assert runs.process_alive(worker) is True
        moment = time.time()
        row = recovery.classify_pointer(
            pointer, now_seconds=moment, stale_after_seconds=3600
        )
        snapshot = recovery._watch_snapshot(pointer, moment=moment, stall_seconds=3600)
        assert row["process_alive"] is True
        assert row["classification"] == "running"
        assert snapshot["state"] in {"running", "working"}
        assert (
            snapshot["recovery_classification"]
            == row["fleet_verdict"]["recovery_classification"]
        )
        folded, _ = recovery.fleet_transitions(
            {pointer["run_id"]: before}, {pointer["run_id"]: snapshot}
        )
        assert len(folded) == 1
        observed, previous, state, counts = folded[0]
        event = recovery._watch_transition(
            "liveness-fixture",
            kind="transition",
            snapshot=observed,
            previous=previous,
            current=state,
            counts=counts,
            spend_runs=[],
            rate_statuses={},
        )
        assert event["to_state"] == "working"
        assert (
            event["recovery_classification"]
            == row["fleet_verdict"]["recovery_classification"]
        )
        assert event["working"] == 1
        assert event["blocked"] == 0
        print(
            f"supervisor_alive=False worker_alive=True commits=1 classifier={row['classification']} snapshot={snapshot['state']} transition={event['to_state']}"
        )


STUB_CASES = [
    case
    for name, case in vars(liveness).items()
    if name.startswith("test_")
    and callable(case)
    and "tmp_path" in inspect.signature(case).parameters
]


@pytest.mark.parametrize("case", STUB_CASES, ids=lambda case: case.__name__)
def test_every_liveness_stub_has_producer_classifier_parity(
    case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = []

    def compare(pointer: dict, *, snapshot_result: bool):
        moment = time.time()
        row = recovery.classify_pointer(
            pointer, now_seconds=moment, stale_after_seconds=3600
        )
        snapshot = recovery._watch_snapshot(pointer, moment=moment, stall_seconds=3600)
        observed.append(pointer["run_id"])
        assert (
            snapshot["recovery_classification"]
            == row["fleet_verdict"]["recovery_classification"]
        ), (
            pointer["run_id"],
            snapshot["recovery_classification"],
            row["fleet_verdict"]["recovery_classification"],
        )
        assert snapshot["recovery"] == row["fleet_verdict"]["recovery"]
        assert snapshot["classification"] == row["classification"]
        assert snapshot["process_alive"] == row["process_alive"]
        assert snapshot["state"] == row["fleet_verdict"]["state"]
        run_id = pointer["run_id"]
        folded, _ = recovery.fleet_transitions(
            {run_id: {**snapshot, "state": "unknown"}}, {run_id: snapshot}
        )
        assert len(folded) == 1
        emitted, previous, state, counts = folded[0]
        event = recovery._watch_transition(
            "liveness-fixture",
            kind="transition",
            snapshot=emitted,
            previous=previous,
            current=state,
            counts=counts,
            spend_runs=[],
            rate_statuses={},
        )
        assert event["classification"] == row["classification"]
        assert event["process_alive"] == row["process_alive"]
        assert event["to_state"] == row["fleet_verdict"]["state"]
        assert (
            event["recovery_classification"]
            == row["fleet_verdict"]["recovery_classification"]
        )
        return snapshot if snapshot_result else row

    monkeypatch.setattr(
        liveness, "_classify", lambda pointer: compare(pointer, snapshot_result=False)
    )
    monkeypatch.setattr(
        liveness, "_snapshot", lambda pointer: compare(pointer, snapshot_result=True)
    )
    kwargs = {"tmp_path": tmp_path}
    if "monkeypatch" in inspect.signature(case).parameters:
        kwargs["monkeypatch"] = monkeypatch
    case(**kwargs)
    assert observed, (
        "The parity instrument must observe the stub's actual classifier read"
    )


def test_snapshot_and_transition_do_not_rederive_the_classifier_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with liveness._live_child() as worker:
        pointer = liveness._pointer(tmp_path, "r-one-verdict", pid=worker)
        moment = time.time()
        row = recovery.classify_pointer(
            pointer, now_seconds=moment, stale_after_seconds=3600
        )

        def refuse(*args, **kwargs):
            pytest.fail(
                "The producer probed evidence after consuming the classifier verdict"
            )

        calls = []

        def classified(record, **kwargs):
            calls.append(record)
            assert kwargs == {"now_seconds": moment, "stale_after_seconds": 3600}
            return row

        monkeypatch.setattr(recovery, "classify_pointer", classified)
        for name in (
            "_watch_verdict",
            "_newest_stream_last_record_type",
            "_run_stream_quiet_seconds",
            "_stall_wait_reason",
            "_worker_record_liveness",
        ):
            monkeypatch.setattr(recovery, name, refuse)
        monkeypatch.setattr(runs, "record_process_alive", refuse)
        monkeypatch.setattr(runs, "process_alive", refuse)
        snapshot = recovery._watch_snapshot(pointer, moment=moment, stall_seconds=3600)
        assert calls == [pointer]
        folded, _ = recovery.fleet_transitions(
            {pointer["run_id"]: {**snapshot, "state": "dispatched"}},
            {pointer["run_id"]: snapshot},
        )
        assert len(folded) == 1
        assert folded[0][2] == row["fleet_verdict"]["state"]
        assert snapshot["classification"] == row["classification"]
        assert snapshot["process_alive"] == row["process_alive"]
