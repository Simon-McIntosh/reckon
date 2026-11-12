"""Scheduler measurements release the landing pass and survive its restart."""

from __future__ import annotations

import json
import shlex
import subprocess
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main
from reckon.crew import landing_measure, queue_landing, runs
from reckon.crew.admission import landing as admission_landing
from tests import test_the_landing_pass_measures_and_lands as deliveries


class Scheduler:
    """The submitter returns now; only the test can finish one of its jobs."""

    def __init__(self):
        self.jobs = {}
        self.release = threading.Event()

    def run(self, argv, **options):
        if argv[0] == "sbatch":
            assert not any(key.startswith("SLURM_") for key in options["env"])
            step = shlex.split(argv[-1])
            script = step[step.index("-c") + 1]
            request = Path(shlex.split(script.splitlines()[-1])[-1])
            job_id = str(7000 + len(self.jobs))
            self.jobs[job_id] = {"state": "RUNNING", "request": request}
            if "--wait" in argv:
                self.release.wait(15)
            return subprocess.CompletedProcess(argv, 0, job_id + "\n", "")
        job_id = argv[argv.index("-j") + 1]
        state = self.jobs[job_id]["state"]
        if argv[0] == "squeue" and state != "RUNNING":
            state = ""
        assert argv[0] in {"squeue", "sacct"}
        return subprocess.CompletedProcess(argv, 0, state + "\n", "")

    def finish(self, job_id, state="COMPLETED", *, receipt="EXIT=0\n"):
        job = self.jobs[job_id]
        job["state"] = state
        path = job["request"]
        request = json.loads(path.read_text())
        measured = _clean_measurement(request["head"], request["primary"])
        (path.parent / "result.json").write_text(json.dumps(measured.as_dict()))
        (path.parent / "job.log").write_text("measurement finished\n" + receipt)


def _clean_measurement(head, primary):
    arm = {"command": ["python", "-m", "pytest"], "exit_status": 0, "failure_ids": []}
    return landing_measure.Measurement(
        command=deliveries.SUITE,
        head=head,
        project=deliveries.PROJECT,
        suite={"base": arm, "head": arm},
        population={"main_sha": primary, "candidate_sha": head, "files": []},
    )


@pytest.fixture()
def fleet(tmp_path, monkeypatch):
    delivered = deliveries.delivery.__wrapped__(tmp_path, monkeypatch)
    scheduler = Scheduler()
    landed = []
    monkeypatch.setenv(landing_measure.MEASUREMENT_WIDTH_ENV, "2")
    monkeypatch.setattr(landing_measure, "_SCHEDULER_RUN", scheduler.run)
    monkeypatch.setattr(
        queue_landing,
        "_measure_config",
        lambda _pointer: {"landing": {"measure_partition": "all_debug"}},
    )
    monkeypatch.setattr(queue_landing, "_landing_code_stamp", lambda: "fixture-code")
    monkeypatch.setattr(queue_landing, "_landing_beat", lambda *_a, **_k: "")

    def land(run_id):
        landed.append(run_id)
        runs.pointer_path(run_id).unlink()
        return True, ""

    monkeypatch.setattr(queue_landing, "_land", land)
    for run_id, order in (("r-second", 1), ("r-third", 2)):
        deliveries._deliver_another(run_id, order=order)
    for run_id in (deliveries.RUN, "r-second", "r-third"):
        deliveries._accept(
            base=delivered["base"], head=delivered["head"], run_id=run_id
        )
    return {**delivered, "scheduler": scheduler, "landed": landed}


def _cache_third(fleet):
    queue_landing._record_suite_evidence(
        "r-third", _clean_measurement(fleet["head"], fleet["head"])
    )


def test_one_pass_submits_both_measurements_and_lands_the_ready_run(fleet):
    _cache_third(fleet)
    done = threading.Event()
    result = {}
    errors = []

    def drive():
        try:
            result.update(queue_landing.land_once())
        except Exception as exc:  # noqa: BLE001 — propagate the thread failure to pytest
            errors.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=drive, daemon=True)
    thread.start()
    try:
        assert done.wait(3), "the pass waited for its measurement; third run unlanded"
        assert not errors, errors
        assert len(fleet["scheduler"].jobs) == 2
        assert result["landed"] == ["r-third"]
        assert result["deferred"] == [deliveries.RUN, "r-second"]
        assert all(
            job["state"] == "RUNNING" for job in fleet["scheduler"].jobs.values()
        )
    finally:
        fleet["scheduler"].release.set()
        thread.join(10)
    assert not thread.is_alive()


def test_a_later_pass_consumes_only_the_completed_job(fleet):
    _cache_third(fleet)
    queue_landing.land_once()
    fleet["scheduler"].finish("7000")
    result = queue_landing.land_once()
    assert result["landed"] == [deliveries.RUN]
    assert result["deferred"] == ["r-second"]
    assert len(fleet["scheduler"].jobs) == 2
    assert runs.read_pointer("r-second")["landing_measurement_job"]["job_id"] == "7001"


@pytest.mark.parametrize("state", ["FAILED", "TIMEOUT", "CANCELLED", "OUT_OF_MEMORY"])
def test_a_failed_job_refuses_only_its_delivery(fleet, state):
    queue_landing.land_once()
    job = runs.read_pointer(deliveries.RUN)["landing_measurement_job"]
    fleet["scheduler"].finish("7000", state)
    fleet["scheduler"].finish("7001")
    result = queue_landing.land_once()
    assert result["landed"] == ["r-second"]
    assert len(result["obligations"]) == 1
    guard = result["obligations"][0]["guard"]
    assert result["obligations"][0]["run_id"] == deliveries.RUN
    assert job["job_id"] in guard and job["log_path"] in guard and state in guard
    assert runs.read_pointer(deliveries.RUN)["admission_landing"]["status"] == "refused"


def test_width_two_submits_exactly_two_of_three_measurements(fleet):
    result = queue_landing.land_once()
    assert len(fleet["scheduler"].jobs) == 2
    assert len(result["deferred"]) == 3
    assert not result["landed"]
    assert "landing_measurement_job" not in runs.read_pointer("r-third")


def test_a_fresh_lander_adopts_the_recorded_job(fleet):
    _cache_third(fleet)
    queue_landing.land_once()
    # A fresh worker has no in-memory submission state. The persisted pointer
    # is its only input about the two jobs the first worker left running.
    worker = admission_landing._LandingWorker()
    assert worker.tick()
    assert worker.settle(3)["deferred"] == [deliveries.RUN, "r-second"]
    assert len(fleet["scheduler"].jobs) == 2
    fleet["scheduler"].finish("7000")
    worker = admission_landing._LandingWorker()
    assert worker.tick()
    assert worker.settle(3)["landed"] == [deliveries.RUN]
    assert len(fleet["scheduler"].jobs) == 2


@pytest.mark.parametrize("receipt", ["", "EXIT=1\n", "EXIT=0\nstill writing\n"])
def test_a_completed_scheduler_job_needs_its_own_success_receipt(fleet, receipt):
    _cache_third(fleet)
    queue_landing.land_once()
    fleet["scheduler"].finish("7000", receipt=receipt)
    result = queue_landing.land_once()
    assert result["landed"] == []
    assert "7000" in result["obligations"][0]["guard"]
    assert "receipt" in result["obligations"][0]["guard"]


def test_an_unknown_job_keeps_its_slot_and_is_not_resubmitted(fleet):
    queue_landing.land_once()
    fleet["scheduler"].jobs["7000"]["state"] = ""
    result = queue_landing.land_once()
    assert len(fleet["scheduler"].jobs) == 2
    assert len(result["deferred"]) == 3
    assert not result["obligations"]


def test_admit_once_returns_while_its_measurement_is_still_running(fleet, monkeypatch):
    from reckon.crew.admission import release, selection, service

    for run_id in ("r-second", "r-third"):
        runs.pointer_path(run_id).unlink()
    monkeypatch.setattr(service, "_admit_code_stamp", lambda: "fixture-code")
    monkeypatch.setattr(release, "_release_tick", dict)
    monkeypatch.setattr(selection, "admit_once", lambda **_k: {"verdicts": []})
    result = CliRunner().invoke(main, ["crew", "admit", "--once"])
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    assert receipt["landing"]["deferred"] == [deliveries.RUN]
    assert fleet["scheduler"].jobs["7000"]["state"] == "RUNNING"
    assert (
        runs.read_pointer(deliveries.RUN)["landing_measurement_job"]["job_id"] == "7000"
    )


def test_the_job_measures_pinned_revisions_inside_its_own_allocation(
    fleet, monkeypatch
):
    queue_landing.land_once()
    request_path = fleet["scheduler"].jobs["7000"]["request"]
    request = json.loads(request_path.read_text())
    monkeypatch.setattr(landing_measure, "base_checkout", deliveries.REAL_BASE_CHECKOUT)
    seen = []

    def measure(**options):
        assert not options["config"]["landing"]["measure_partition"]
        assert options["base_sha"] == request["primary"]
        assert options["head_sha"] == request["head"]
        assert options["tree"] != fleet["repo"]
        assert options["tree"].is_dir() and options["base_tree"].is_dir()
        assert options["logs_dir"] == request_path.parent
        seen.append(options)
        return _clean_measurement(request["head"], request["primary"])

    monkeypatch.setattr(landing_measure, "measure_run", measure)
    queue_landing.run_measurement_request(request_path)
    result = json.loads((request_path.parent / "result.json").read_text())
    assert len(seen) == 1
    assert result["execution"] == "slurm"
    assert result["population"]["main_sha"] == request["primary"]
    assert result["population"]["candidate_sha"] == request["head"]
    assert len(fleet["scheduler"].jobs) == 2
    assert not (fleet["scratch_root"] / "landing-base").exists()


def test_publication_records_reuse_the_completed_measurement(fleet, monkeypatch):
    head = fleet["head"]
    run_id = deliveries.RUN
    queue_landing._record_suite_evidence(run_id, _clean_measurement(head, head))
    repo = fleet["repo"]
    (repo / "docs" / "publication.txt").write_text("publication records\n")
    deliveries._git(repo, "add", "docs/publication.txt")
    deliveries._git(repo, "commit", "-qm", "docs: record publication")
    primary = deliveries._git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(
        queue_landing,
        "_measure_delivery",
        lambda *_a, **_k: pytest.fail("publication waited for another measurement"),
    )
    candidate = queue_landing._promoted_candidate(
        runs.read_pointer(run_id), run_id, head, str(repo)
    )
    assert deliveries._git(repo, "rev-parse", candidate + "^1") == primary
    assert deliveries._git(repo, "rev-parse", candidate + "^2") == head
    record = json.loads(queue_landing._measurement_path(run_id).read_text())
    assert record["population"]["main_sha"] == primary
    assert record["population"]["candidate_sha"] == candidate
    assert fleet["scheduler"].jobs == {}
