"""A landing measure's bound follows its own history, and a timeout is retried.

The measured thing is the bound itself. A fixed figure refuses a run whose
population is simply bigger or slower than the figure assumed, and the refusal
is sticky because neither the code nor the review moves when the host is busy.
These cases drive the bound from recorded arm durations and drive the refusal
clock from recorded failures.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon.crew import landing_measure, queue_landing, runs

PROJECT = "bounded-measure-fixture"
RUN = "r-bounded-measure-implementation"
HEAD = "a" * 40
BASE = "b" * 40

_TIMEOUT_GUARD = (
    "landing measure unreadable: the population base arm exceeded its 1800s bound"
)
_CHECKOUT_GUARD = (
    "landing measure unreadable: the base checkout could not be built: "
    "could not check out bbbb for the base arm: fatal: bad revision"
)
_REGRESSION_GUARD = (
    "added suite regression: tests/test_alpha.py failed 3/3 at head and 0/3 at base"
)


def _arm_record(
    elapsed: float, file_count: int, *, exit_status: int | None = 0
) -> dict[str, object]:
    return {
        "command": ["pytest"],
        "exit_status": exit_status,
        "failure_ids": [],
        "log_path": "",
        "timed_out": exit_status is None,
        "elapsed_seconds": elapsed,
        "file_count": file_count,
    }


def _stored_measurement(
    *,
    elapsed: float,
    file_count: int,
    project: str = PROJECT,
    section: str = "population",
) -> dict[str, object]:
    """One stored measurement as a later bound reads it."""
    return {
        "command": "pytest {node_test_files}",
        "head": HEAD,
        "project": project,
        "suite": {"base": _arm_record(10.0, 1), "head": _arm_record(10.0, 1)},
        section: {
            "files": [f"tests/test_{n}.py" for n in range(file_count)],
            "base": _arm_record(elapsed, file_count),
            "head": _arm_record(elapsed, file_count),
        },
        "isolated": {},
        "real": [],
        "unreadable": "",
    }


def _population(files: tuple[str, ...]):
    return lambda *_args, **_kwargs: files


def _driver(seen: list[float | None]):
    """A command runner that records the bound each arm was given."""

    def run(argv, **kwargs):
        seen.append(kwargs.get("timeout"))
        return subprocess.CompletedProcess(argv, 0, "", "")

    return run


def _measure(tmp_path: Path, seen: list[float | None], *, stored, file_count: int):
    return landing_measure.measure_run(
        command="pytest {node_test_files}",
        tree=tmp_path / "head",
        base_sha=BASE,
        head_sha=HEAD,
        base_tree=tmp_path / "base",
        logs_dir=tmp_path / "logs",
        test_files=("tests/test_node.py",),
        run=_driver(seen),
        stored=stored,
        project=PROJECT,
    )


def test_an_arm_is_bounded_above_what_its_population_last_took(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A population that last took 900 s is not judged again under 600 s."""

    monkeypatch.setattr(landing_measure, "changed_paths", lambda *_a, **_k: ())
    monkeypatch.setattr(
        landing_measure,
        "importer_population",
        _population(tuple(f"tests/test_{n}.py" for n in range(100))),
    )
    seen: list[float | None] = []
    measured = _measure(
        tmp_path,
        seen,
        stored=[_stored_measurement(elapsed=900.0, file_count=100)],
        file_count=100,
    )

    population_bounds = seen[-2:]
    assert (
        population_bounds
        == [
            landing_measure.arm_bound(
                landing_measure.DEFAULT_MEASURE_TIMEOUT_SECONDS,
                100,
                landing_measure.median_seconds_per_file(
                    [_stored_measurement(elapsed=900.0, file_count=100)]
                ),
            )
        ]
        * 2
    )
    assert min(population_bounds) > 900.0
    assert min(population_bounds) > landing_measure.DEFAULT_MEASURE_TIMEOUT_SECONDS
    assert measured.unreadable == ""


def test_an_arm_with_no_history_is_bounded_by_the_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(landing_measure, "changed_paths", lambda *_a, **_k: ())
    monkeypatch.setattr(
        landing_measure,
        "importer_population",
        _population(tuple(f"tests/test_{n}.py" for n in range(100))),
    )
    seen: list[float | None] = []
    _measure(tmp_path, seen, stored=[], file_count=100)

    assert seen == [landing_measure.DEFAULT_MEASURE_TIMEOUT_SECONDS] * 4


def test_only_the_projects_own_measurements_size_its_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another repository's seconds per file measure a different suite."""

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    record = _stored_measurement(elapsed=900.0, file_count=100, project="elsewhere")
    path = runs.run_dir("r-elsewhere") / "landing-measure" / "measurement.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    pointer = {"run_id": "r-elsewhere", "project": "elsewhere"}

    assert queue_landing._pass_history([pointer], PROJECT) == []
    assert queue_landing._pass_history([pointer], "elsewhere") == [record]


def test_every_arm_records_its_elapsed_seconds_and_file_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(landing_measure, "changed_paths", lambda *_a, **_k: ())
    monkeypatch.setattr(
        landing_measure,
        "importer_population",
        _population(tuple(f"tests/test_{n}.py" for n in range(7))),
    )
    seen: list[float | None] = []
    measured = _measure(tmp_path, seen, stored=[], file_count=7)
    runs._write_json(runs.pointer_path(RUN), {"run_id": RUN, "project": PROJECT})
    queue_landing._record_suite_evidence(RUN, measured)

    stored = json.loads(
        (runs.run_dir(RUN) / "landing-measure" / "measurement.json").read_text(
            encoding="utf-8"
        )
    )
    for section in ("suite", "population"):
        for arm in ("base", "head"):
            record = stored[section][arm]
            assert isinstance(record["elapsed_seconds"], float)
            assert record["elapsed_seconds"] >= 0.0
            assert isinstance(record["file_count"], int)
    assert stored["population"]["base"]["file_count"] == 7
    assert stored["suite"]["base"]["file_count"] == 1
    assert stored["project"] == PROJECT


class _Yield:
    """A context manager handing back a prepared tree, for the base arm."""

    def __init__(self, tree: Path) -> None:
        self._tree = tree

    def __enter__(self) -> Path:
        return self._tree

    def __exit__(self, *_exception: object) -> bool:
        return False


def _clean_measurement() -> landing_measure.Measurement:
    return landing_measure.Measurement(
        command="pytest {node_test_files}",
        suite={},
        population={},
        head=HEAD,
        project=PROJECT,
    )


def test_a_pass_reads_the_projects_history_once_for_every_run_it_measures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runs directory is thousands of entries on shared storage.

    The pass reads one measurement record per run of its own project and hands
    the same history to every run it measures, so the reads do not grow with
    the directory's population nor multiply by the number of measured runs.
    """

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    for n in range(60):
        path = runs.run_dir(f"r-unrelated-{n}") / "landing-measure"
        path.mkdir(parents=True)
        (path / "measurement.json").write_text(
            json.dumps(_stored_measurement(elapsed=5.0, file_count=1)), encoding="utf-8"
        )
    pointers = []
    for n in range(3):
        run_id = f"r-mine-{n}"
        directory = runs.run_dir(run_id) / "landing-measure"
        directory.mkdir(parents=True)
        (directory / "measurement.json").write_text(
            json.dumps(_stored_measurement(elapsed=900.0, file_count=100)),
            encoding="utf-8",
        )
        runs._write_json(runs.pointer_path(run_id), {"run_id": run_id})
        pointers.append({"run_id": run_id, "project": PROJECT})

    reads: list[str] = []
    original = Path.read_text

    def counting_read_text(self, *args, **kwargs):
        if self.name == "measurement.json":
            reads.append(str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    history = queue_landing._pass_history(pointers, PROJECT)
    after_history = len(reads)

    assert after_history == len(pointers) == 3
    assert after_history < 60

    base_tree = tmp_path / "base"
    base_tree.mkdir()
    monkeypatch.setattr(
        landing_measure, "base_checkout", lambda *_a, **_k: _Yield(base_tree)
    )
    monkeypatch.setattr(
        landing_measure, "measure_run", lambda **_kwargs: _clean_measurement()
    )
    for pointer in pointers:
        queue_landing._measure_delivery(
            {
                **pointer,
                "repo": str(tmp_path),
                "base_sha": BASE,
                "suite_command": "pytest {node_test_files}",
                "node": {"id": "implementation"},
            },
            str(pointer["run_id"]),
            HEAD,
            str(tmp_path / "head"),
            history,
        )

    # Measuring three runs added no history read at all.
    assert len(reads) == after_history


def _refusal(
    guard: str, *, failed_at: float = 1_000.0, attempts: int = 1
) -> dict[str, object]:
    return {
        "head": HEAD,
        "guard": guard,
        "status": "refused",
        "code_stamp": "the-rule",
        "review_stamp": "the-record",
        "attempts": attempts,
        "failed_at": failed_at,
    }


def test_a_timeout_refusal_is_judged_again_after_its_backoff() -> None:
    recorded = _refusal(_TIMEOUT_GUARD)
    backoff = queue_landing._retry_backoff(int(recorded["attempts"]))

    assert queue_landing._refusal_holds(
        recorded, "the-rule", "the-record", now=1_000.0 + backoff - 1.0
    )
    assert not queue_landing._refusal_holds(
        recorded, "the-rule", "the-record", now=1_000.0 + backoff + 1.0
    )


def test_a_base_checkout_refusal_is_judged_again_after_its_backoff() -> None:
    recorded = _refusal(_CHECKOUT_GUARD)
    backoff = queue_landing._retry_backoff(int(recorded["attempts"]))

    assert queue_landing._refusal_holds(
        recorded, "the-rule", "the-record", now=1_000.0 + backoff - 1.0
    )
    assert not queue_landing._refusal_holds(
        recorded, "the-rule", "the-record", now=1_000.0 + backoff + 1.0
    )


def test_an_added_regression_is_not_judged_again_until_the_code_or_review_moves() -> (
    None
):
    recorded = _refusal(_REGRESSION_GUARD)
    far_beyond_any_backoff = 1_000.0 + 100 * queue_landing.RETRY_BACKOFF_CAP_SECONDS

    assert queue_landing._refusal_holds(
        recorded, "the-rule", "the-record", now=far_beyond_any_backoff
    )
    assert not queue_landing._refusal_holds(
        recorded, "a-new-rule", "the-record", now=far_beyond_any_backoff
    )
    assert not queue_landing._refusal_holds(
        recorded, "the-rule", "a-new-record", now=far_beyond_any_backoff
    )


def test_the_backoff_doubles_across_repeated_timeouts_and_is_capped() -> None:
    base = queue_landing.RETRY_BACKOFF_SECONDS
    cap = queue_landing.RETRY_BACKOFF_CAP_SECONDS

    assert [queue_landing._retry_backoff(n) for n in (1, 2, 3, 4)] == [
        base,
        2 * base,
        4 * base,
        8 * base,
    ]
    assert queue_landing._retry_backoff(200) == cap
    assert queue_landing._retry_backoff(2) > queue_landing._retry_backoff(1)


def test_a_repeated_timeout_counts_its_attempts_and_another_guard_restarts_them() -> (
    None
):
    previous = _refusal(_TIMEOUT_GUARD, attempts=3)

    assert (
        queue_landing._refusal_attempts(previous, head=HEAD, guard=_TIMEOUT_GUARD) == 4
    )
    assert (
        queue_landing._refusal_attempts(previous, head=HEAD, guard=_REGRESSION_GUARD)
        == 1
    )


def test_a_timed_out_measurement_is_measured_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stored refusal must not stand in for the retry it asks for."""

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    path = queue_landing._measurement_path(RUN)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "command": "pytest {node_test_files}",
                "head": HEAD,
                "project": PROJECT,
                "suite": {},
                "population": {},
                "isolated": {},
                "real": [],
                "unreadable": (
                    "the population base arm exceeded its "
                    f"{landing_measure.DEFAULT_MEASURE_TIMEOUT_SECONDS:g}s bound"
                ),
            }
        ),
        encoding="utf-8",
    )

    assert (
        queue_landing._stored_measurement(RUN, HEAD, "pytest {node_test_files}") is None
    )
