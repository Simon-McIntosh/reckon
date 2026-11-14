"""Landing compares current main with the merged importers, including new callers."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon.crew import (
    landing_measure,
    merge,
    queue_landing,
    review_dispositions,
    runs,
)
from tests import test_queue_landing as cli_checks

RUN = "r-population-delivery"
FAILURE = "tests/test_consumer.py::test_integration"
COMMAND = "python -m pytest -q tests/test_node.py"


def _git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE
    ).strip()


def _commit(repo, path, content):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(repo, "add", path)
    _git(
        repo,
        "commit",
        "-qm",
        "test: prepare integration input\n\nRecord the fixture input.",
    )
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _commit(repo, "subject.py", "VALUE = 'base'\n")
    base = _commit(repo, "tests/test_node.py", "def test_node():\n    pass\n")
    with landing_measure.base_checkout(repo, base) as worker:
        head = _commit(worker, "subject.py", "VALUE = 'delivered'\n")
        main = _commit(
            repo,
            "tests/test_consumer.py",
            "import subject\n\ndef test_integration():\n    pass\n",
        )
        pointer = {
            "run_id": RUN,
            "project": "population-fixture",
            "role": "implement",
            "session": "owner",
            "repo": str(repo),
            "worktree": str(worker),
            "base_sha": base,
            "suite_command": COMMAND,
            "node": {
                "id": "population",
                "role": "implement",
                "write_paths": ["subject.py", "tests/test_node.py"],
            },
        }
        runs._write_json(runs.pointer_path(RUN), pointer)
        monkeypatch.setattr(runs, "list_live", lambda: [runs.read_pointer(RUN)])
        monkeypatch.setattr(
            queue_landing.recovery,
            "classify_pointer",
            lambda _p: {
                "manifest_status": "complete",
                "manifest_commits": [head],
            },
        )
        monkeypatch.setattr(queue_landing, "_owed_review_tiers", lambda _p: ["code"])
        monkeypatch.setattr(
            queue_landing, "_review_for_tier", lambda *_a: ({"head": head}, "")
        )
        monkeypatch.setattr(queue_landing, "_review_tier_is_complete", lambda *_a: True)
        monkeypatch.setattr(
            review_dispositions, "record_landed_deferrals", lambda *_a, **_k: None
        )
        monkeypatch.setattr(queue_landing, "_landing_beat", lambda *_a: "")
        monkeypatch.setattr(queue_landing, "_measure_timeout", lambda _p: 30.0)
        landed = []
        monkeypatch.setattr(
            queue_landing, "_land", lambda run: (landed.append(run) is None, "")
        )
        yield {
            "repo": repo,
            "worker": worker,
            "base": base,
            "head": head,
            "main": main,
            "landed": landed,
            "calls": [],
        }


def _runner(delivery, monkeypatch, *, repeats_fail=True, main_fails=False, clean=False):
    real_run = subprocess.run

    def run(argv, **kwargs):
        if argv[0] == "git":
            return real_run(argv, **kwargs)
        tree = Path(kwargs["cwd"])
        consumer = (tree / "tests/test_consumer.py").is_file()
        delivered = "delivered" in (tree / "subject.py").read_text()
        population = "tests/test_consumer.py" in argv
        isolated = FAILURE in argv
        delivery["calls"].append(
            {
                "population": population,
                "isolated": isolated,
                "consumer": consumer,
                "delivered": delivered,
                "argv": argv,
            }
        )
        fails = consumer and (population or isolated) and not clean
        fails = fails and (main_fails or (delivered and (not isolated or repeats_fail)))
        output = (
            f"FAILED {FAILURE} - AssertionError\n1 failed\n" if fails else "1 passed\n"
        )
        return subprocess.CompletedProcess(argv, int(fails), output, "")

    monkeypatch.setattr(landing_measure, "_RUN", run)


def _record():
    path = runs.run_dir(RUN) / "landing-measure/measurement.json"
    return json.loads(path.read_text())


def test_a_reproducing_candidate_failure_refuses_landing(delivery, monkeypatch):
    _runner(delivery, monkeypatch)
    result = queue_landing.land_once()
    assert not delivery["landed"], result
    assert FAILURE in result["obligations"][0]["guard"]
    record = _record()
    assert record["real"] == [FAILURE]
    assert record["isolated"][FAILURE] == {"base": [False] * 3, "head": [True] * 3}
    assert record["population"]["main_sha"] == delivery["main"]
    candidate = record["population"]["candidate_sha"]
    assert _git(delivery["repo"], "rev-parse", candidate + "^1") == delivery["main"]
    assert _git(delivery["repo"], "rev-parse", candidate + "^2") == delivery["head"]
    log = Path(record["population"]["head"]["log_path"])
    assert FAILURE in log.read_text()
    assert str(log) in result["obligations"][0]["guard"]


def test_an_added_failure_passing_serial_reruns_lands_as_a_flake(delivery, monkeypatch):
    _runner(delivery, monkeypatch, repeats_fail=False)
    result = queue_landing.land_once()
    assert result["landed"] == [RUN]
    assert _record()["flakes"] == [FAILURE]
    assert _record()["real"] == []


def test_a_clean_candidate_lands_after_both_population_arms(delivery, monkeypatch):
    _runner(delivery, monkeypatch, clean=True)
    result = queue_landing.land_once()
    assert result["landed"] == [RUN]
    calls = [c for c in delivery["calls"] if c["population"]]
    assert {(c["consumer"], c["delivered"]) for c in calls} == {
        (True, False),
        (True, True),
    }
    assert _record()["real"] == []
    assert _git(delivery["repo"], "rev-parse", "HEAD") == delivery["main"]
    assert _git(delivery["worker"], "rev-parse", "HEAD") == delivery["head"]


def test_a_failure_shared_with_current_main_is_not_added(delivery, monkeypatch):
    _runner(delivery, monkeypatch, main_fails=True)
    result = queue_landing.land_once()
    assert result["landed"] == [RUN]
    record = _record()
    assert record["population"]["base"]["failure_ids"] == [FAILURE]
    assert record["population"]["head"]["failure_ids"] == [FAILURE]
    assert record["isolated"] == {}
    assert record["real"] == []


def test_skipping_the_population_lands_the_reproducing_failure(delivery, monkeypatch):
    _runner(delivery, monkeypatch)
    monkeypatch.setattr(landing_measure, "importer_population", lambda *_a: ())
    result = queue_landing.land_once()
    assert result["landed"] == [RUN]
    assert not any(c["population"] for c in delivery["calls"])


def test_a_measurement_for_another_primary_is_not_reused(delivery, monkeypatch):
    _runner(delivery, monkeypatch, clean=True)
    queue_landing.land_once()
    before = _record()
    _commit(delivery["repo"], "reference.txt", "primary advanced\n")
    runs._mutate_pointer(
        RUN, lambda p: {k: v for k, v in p.items() if k != "admission_landing"}
    )
    _runner(delivery, monkeypatch)

    result = queue_landing.land_once()

    assert not result["landed"]
    assert FAILURE in result["obligations"][0]["guard"]
    assert _record()["population"]["main_sha"] != before["population"]["main_sha"]


def test_a_primary_change_rejudges_a_population_refusal(delivery, monkeypatch):
    _runner(delivery, monkeypatch)
    assert not queue_landing.land_once()["landed"]
    _commit(delivery["repo"], "reference.txt", "primary repaired\n")
    _runner(delivery, monkeypatch, clean=True)

    assert queue_landing.land_once()["landed"] == [RUN]


def test_a_population_runner_error_is_not_a_clean_measurement(delivery, monkeypatch):
    _runner(delivery, monkeypatch, clean=True)
    runner = landing_measure._RUN

    def broken(argv, **kwargs):
        if "tests/test_consumer.py" in argv:
            return subprocess.CompletedProcess(argv, 2, "collection interrupted\n", "")
        return runner(argv, **kwargs)

    monkeypatch.setattr(landing_measure, "_RUN", broken)
    result = queue_landing.land_once()
    assert not result["landed"]
    assert "unreadable" in result["obligations"][0]["guard"]


def test_a_primary_moving_during_measurement_defers_landing(delivery, monkeypatch):
    _runner(delivery, monkeypatch, clean=True)
    runner = landing_measure._RUN
    moved = []

    def move(argv, **kwargs):
        answer = runner(argv, **kwargs)
        if "tests/test_consumer.py" in argv and not moved:
            moved.append(
                _commit(delivery["repo"], "reference.txt", "primary advanced\n")
            )
        return answer

    monkeypatch.setattr(landing_measure, "_RUN", move)
    result = queue_landing.land_once()
    assert result["landed"] == []
    assert result["deferred"] == [RUN]


def test_an_unreadable_delivery_diff_refuses_without_claiming_no_importers(
    delivery, monkeypatch
):
    _runner(delivery, monkeypatch, clean=True)
    runs._mutate_pointer(RUN, lambda p: {**p, "base_sha": "missing-revision"})
    result = queue_landing.land_once()
    assert not result["landed"]
    assert "delivered changes could not be read" in result["obligations"][0]["guard"]
    assert not delivery["calls"]


@pytest.fixture()
def actual_delivery(tmp_path, monkeypatch):
    return cli_checks.promotable_delivery.__wrapped__(tmp_path, monkeypatch)


def test_primary_advance_after_measurement_refuses_the_actual_cli_merge(
    actual_delivery, monkeypatch
):
    delivery = actual_delivery
    moved = []

    def advance(command, **options):
        moved.append(
            _commit(delivery["repo"], "concurrent.txt", "concurrent primary edit\n")
        )
        return delivery["run_in_shim"](command, **options)

    monkeypatch.setattr(queue_landing, "_run_srun", advance)
    result = queue_landing.land_once()

    assert not result["landed"], result
    assert "primary" in result["obligations"][0]["guard"]
    assert _git(delivery["repo"], "rev-parse", "HEAD") == moved[0]
    assert _git(delivery["origin"], "rev-parse", "main") == delivery["base"]
    assert runs.pointer_path(delivery["run_id"]).exists()


def test_the_actual_cli_lands_exactly_the_measured_candidate(actual_delivery):
    delivery = actual_delivery
    result = queue_landing.land_once()

    assert result["landed"] == [delivery["run_id"]], (result, delivery["receipts"])
    record = queue_landing._measurement_record(delivery["run_id"])
    candidate = record["population"]["candidate_sha"]
    main = record["population"]["main_sha"]
    assert main != delivery["base"]  # Promotion commits precede the final measurement.
    assert _git(delivery["repo"], "rev-parse", candidate + "^1") == main
    assert _git(delivery["repo"], "rev-parse", candidate + "^2") == delivery["head"]
    _git(delivery["repo"], "merge-base", "--is-ancestor", candidate, "HEAD")
    assert (
        _git(delivery["repo"], "show", candidate + ":reviewed.txt")
        == "accepted delivery"
    )
    assert _git(delivery["origin"], "rev-parse", "main") == _git(
        delivery["repo"], "rev-parse", "HEAD"
    )
    receipt = json.loads(delivery["receipts"][-1]["output"])
    assert receipt["steps"]["promote"]["ok"] is True
    assert receipt["steps"]["land"]["commit"] == candidate
    assert not runs.pointer_path(delivery["run_id"]).exists()
    assert not delivery["worker"].exists()
    assert all(
        r["command"][r["command"].index("crew") + 1] != "merge"
        for r in delivery["receipts"]
    )


def test_candidate_handoff_preserves_the_promotion_guard(actual_delivery, monkeypatch):
    delivery = actual_delivery

    def remove_gate(command, **options):
        if "complete" in command:
            delivery["gate_log"].unlink()
        return delivery["run_in_shim"](command, **options)

    monkeypatch.setattr(queue_landing, "_run_srun", remove_gate)
    result = queue_landing.land_once()

    assert not result["landed"], result
    assert "gate" in result["obligations"][0]["guard"]
    assert _git(delivery["repo"], "rev-parse", "HEAD") == delivery["base"]
    assert _git(delivery["origin"], "rev-parse", "main") == delivery["base"]
    assert runs.pointer_path(delivery["run_id"]).exists()
    assert delivery["worker"].exists()


def test_primary_advance_after_promotion_measurement_refuses_publication(
    actual_delivery, monkeypatch
):
    delivery = actual_delivery
    original = merge.land
    moved = []

    def advance(repo, **options):
        moved.append(_commit(repo, "concurrent.txt", "concurrent primary edit\n"))
        return original(repo, **options)

    monkeypatch.setattr(merge, "land", advance)
    result = queue_landing.land_once()

    assert not result["landed"], result
    assert "parents" in result["obligations"][0]["guard"]
    assert _git(delivery["repo"], "rev-parse", "HEAD") == moved[0]
    assert _git(delivery["origin"], "rev-parse", "main") == delivery["base"]
    assert not (delivery["repo"] / "reviewed.txt").exists()
    assert delivery["worker"].exists()
    saved = json.loads(
        (runs.run_dir(delivery["run_id"]) / "admission-landing.json").read_text()
    )
    assert saved["status"] == "refused"
    assert "parents" in saved["guard"]


def test_an_unarmed_implementation_cannot_land_unmeasured(delivery, monkeypatch):
    _runner(delivery, monkeypatch)
    runs._mutate_pointer(
        RUN, lambda p: {k: v for k, v in p.items() if k != "suite_command"}
    )

    result = queue_landing.land_once()

    assert not result["landed"], result
    assert "suite command" in result["obligations"][0]["guard"]
    assert _record()["unreadable"]
    assert not delivery["calls"]


def test_delivery_paths_reuses_the_strict_merge_reader(delivery, monkeypatch):
    owner = merge._changed_paths
    calls = []

    def read(*args):
        calls.append(args)
        return owner(*args)

    monkeypatch.setattr(merge, "_changed_paths", read)
    paths = landing_measure.delivered_paths(
        delivery["repo"], delivery["base"], delivery["head"]
    )

    assert paths == ["subject.py"]
    assert calls == [(delivery["repo"], delivery["base"], delivery["head"])]


def test_delivery_paths_preserves_git_path_boundaries(delivery):
    path = "nested/line\nbreak.py"
    head = _commit(delivery["worker"], path, "VALUE = 1\n")
    paths = landing_measure.delivered_paths(delivery["repo"], delivery["head"], head)
    assert paths == [path]


@pytest.mark.parametrize(
    "source",
    [
        "from reckon.crew import runs, queue_landing\n",
        "from reckon.crew import (\n    runs,\n    queue_landing,\n)\n",
        "from reckon.crew import queue_landing as landing\n",
        "import reckon.crew.queue_landing as landing\n",
        'import importlib\nmodule = importlib.import_module("reckon.crew.queue_landing")\n',
    ],
)
def test_importer_discovery_reads_import_lists(tmp_path, source):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_consumer.py").write_text(source)
    assert landing_measure.importer_population(
        ["reckon/crew/queue_landing.py"], tmp_path
    ) == ("tests/test_consumer.py",)


def test_importer_discovery_distinguishes_the_owning_package(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_consumer.py").write_text("from reckon.crew import queue_landing\n")
    (tests / "test_unrelated.py").write_text("from unrelated import queue_landing\n")
    assert landing_measure.importer_population(
        ["reckon/crew/queue_landing.py"], tmp_path
    ) == ("tests/test_consumer.py",)


def test_an_unparseable_importer_is_measured_instead_of_discarded(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_consumer.py").write_text(
        "from reckon.crew import (\n queue_landing,\n"
    )
    assert landing_measure.importer_population(
        ["reckon/crew/queue_landing.py"], tmp_path
    ) == ("tests/test_consumer.py",)
