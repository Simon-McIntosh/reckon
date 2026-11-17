"""The station submits the maintenance pass, so no hand command is needed.

A user systemd timer cannot carry the schedule: this cluster refuses lingering,
so a timer stops at logout while the fleet's own long-running service runs for
as long as the fleet does. The station therefore submits a packing pass, as a
scheduler job on a compute partition, for every managed repository whose
recorded pass is older than a day, and submits nothing for one whose pass is
younger.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

MOMENT = datetime(2026, 11, 15, 6, 30, tzinfo=UTC)
A_DAY = timedelta(days=1)
TWO_DAYS = 2 * A_DAY
AN_HOUR = timedelta(hours=1)


class _Sweeps:
    """The station's review-sweep arm, answered without starting a thread."""

    def __call__(self, project: str, *, session: str | None = None, **kwargs) -> dict:
        return {}


class _Duties:
    """The station's derivation arm, without the worker threads it starts.

    A real derivation runs on a daemon thread that outlives the tick. In this
    process it would still be writing when the interpreter shuts down, and a
    daemon thread caught mid-write is what turns a passing run into a crash at
    exit. The tick's calls are recorded instead.
    """

    def __init__(self) -> None:
        self.requests: list[tuple] = []

    def committing(self):
        from contextlib import nullcontext

        return nullcontext()

    def request(self, *args, **kwargs) -> None:
        self.requests.append((args, kwargs))

    def drain(self, timeout=None) -> None:
        return None


class _Reflex:
    """The station's review-reflex arm, without the thread it starts."""

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


class _Station:
    """A station that records the arms it was constructed with.

    ``run_station`` starts beats, claims a lease and ticks on its own threads,
    so the stand-in answers the calls the loop makes around a tick and nothing
    else.
    """

    def __init__(self, store, **kwargs) -> None:
        self.store = store
        self.maintenance = kwargs.get("maintenance")
        self.clock = lambda: MOMENT
        self.duties = _Duties()
        self.reflex = _Reflex()
        self.ticked = 0

    def tick(self, now=None) -> None:
        self.ticked += 1


class _Scheduler:
    """A scheduler that records each submitted script without running it."""

    def __init__(self) -> None:
        self.scripts: list[str] = []
        self.argv: list[str] = []

    def __call__(self, argv, **kwargs) -> subprocess.CompletedProcess[str]:
        self.argv.append(list(argv))
        self.scripts.append(str(kwargs.get("input") or ""))
        return subprocess.CompletedProcess(
            args=list(argv), returncode=0, stdout="4242;cluster\n", stderr=""
        )


def _git(repository: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repository), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _repository(root: Path, name: str) -> Path:
    """A managed repository, with the docs directory a project is mounted at."""

    repository = root / name
    (repository / "docs").mkdir(parents=True)
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    _git(repository, "config", "user.name", "fixture")
    _git(repository, "config", "gc.auto", "0")
    (repository / "README.md").write_text("reachable\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-qm", "the history")
    return repository


def _station(root: Path, home: Path, projects: dict[str, Path], sweep):
    from reckon.crew import station, station_store

    store = station_store.StationStore(root / "station")
    beat = station.SweepBeat(sweep=_Sweeps(), start=lambda action: action())
    return station.Station(
        store,
        home=home,
        projects=lambda: projects,
        sweeps=beat,
        duties=_Duties(),
        maintenance=sweep,
    )


def test_only_the_repository_whose_pass_is_old_is_submitted(tmp_path: Path, home: Path):
    from reckon.crew import repository_maintenance as maintenance

    stale = _repository(tmp_path, "stale")
    fresh = _repository(tmp_path, "fresh")
    # Each repository's own record says when its last pass ran.
    maintenance.maintenance_pass(stale, home=home, now=MOMENT - TWO_DAYS)
    maintenance.maintenance_pass(fresh, home=home, now=MOMENT - AN_HOUR)

    scheduler = _Scheduler()
    sweep = maintenance.MaintenanceSweep(
        home=home,
        clock=lambda: MOMENT,
        submit=lambda repository: maintenance.submit_pass(
            repository, home=home, runner=scheduler
        ),
    )
    node = _station(
        tmp_path,
        home,
        {"stale-project": stale / "docs", "fresh-project": fresh / "docs"},
        sweep,
    )

    node.tick(now=MOMENT)

    assert len(scheduler.scripts) == 1, scheduler.scripts
    script = scheduler.scripts[0]
    assert str(stale) in script
    assert str(fresh) not in script
    # The job is submitted to a compute partition, never to the node the fleet
    # holds: a packing pass is the heavy work this schedule exists to place.
    partitions = [
        line.split("--partition=", 1)[1].split()[0]
        for line in script.splitlines()
        if line.startswith("#SBATCH --partition=")
    ]
    assert partitions == [maintenance.MAINTENANCE_PARTITION], script
    assert partitions[0].endswith("_debug"), partitions
    # The partition is named on the submitting argv too. The station submits
    # from inside the fleet's own allocation, where a launch that names no
    # partition is refused as an implicit nested job.
    assert scheduler.argv == [
        ["sbatch", "--parsable", f"--partition={maintenance.MAINTENANCE_PARTITION}"]
    ], scheduler.argv
    # The grant is minutes, not hours: a bare ``30:00:00`` asks the scheduler
    # for thirty hours, which on a debug partition pends rather than runs.
    times = [
        line.split("--time=", 1)[1]
        for line in script.splitlines()
        if line.startswith("#SBATCH --time=")
    ]
    assert times == ["00:30:00"], script
    assert "#SBATCH --mem=8G" in script
    # The job does not run from inside the repository it packs. ``python -c``
    # puts the working directory first on the import path, so a job started in
    # the checkout would run that checkout's reckon rather than the installed
    # one, and for the reckon checkout itself the repository's own revision
    # would decide how the repository is packed.
    directories = [
        line.split("cd ", 1)[1]
        for line in script.splitlines()
        if line.startswith("cd ")
    ]
    assert len(directories) == 1, script
    working_directory = Path(directories[0]).resolve()
    assert not working_directory.is_relative_to(stale.resolve()), script
    assert not working_directory.is_relative_to(fresh.resolve()), script


def test_a_repository_with_no_record_is_submitted_once(tmp_path: Path, home: Path):
    """A repository never packed has no record, and the first tick submits it."""

    from reckon.crew import repository_maintenance as maintenance

    repository = _repository(tmp_path, "never-packed")
    scheduler = _Scheduler()
    sweep = maintenance.MaintenanceSweep(
        home=home,
        clock=lambda: MOMENT,
        submit=lambda path: maintenance.submit_pass(path, home=home, runner=scheduler),
    )
    node = _station(tmp_path, home, {"never": repository / "docs"}, sweep)

    node.tick(now=MOMENT)

    assert len(scheduler.scripts) == 1, scheduler.scripts
    # A second tick inside the sweep's own check interval submits nothing.
    node.tick(now=MOMENT + timedelta(minutes=2))
    assert len(scheduler.scripts) == 1, scheduler.scripts


def test_the_pass_is_submitted_through_the_fleet_nodes_own_form(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The schedule submits through the fleet node's submission, not its own.

    One submission form is what keeps the partition on the argv and the
    refusal handling in one place: a second sbatch invocation here would drift
    from the form every other partitioned launch uses as soon as either
    changes. The fleet node's own submission is replaced for this test, so a
    schedule that reaches a scheduler client directly records nothing and
    fails here.
    """

    from reckon.crew import fleet_node
    from reckon.crew import repository_maintenance as maintenance

    calls: list[dict] = []

    def recording_submit(script, *, partition=None, runner=None) -> str:
        calls.append({"script": script, "partition": partition})
        return "4242"

    monkeypatch.setattr(fleet_node, "submit", recording_submit)
    repository = _repository(tmp_path, "one")

    row = maintenance.submit_pass(repository, partition="all_debug", home=home)

    assert row["job_id"] == "4242", row
    assert row["partition"] == "all_debug", row
    assert len(calls) == 1, calls
    assert calls[0]["partition"] == "all_debug", calls
    assert str(repository) in calls[0]["script"], calls


def test_the_station_the_running_service_builds_carries_the_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The station the fleet service starts holds a maintenance sweep.

    The tests around this one hand the station a sweep of their own, so the
    construction that gives the *running* station one is this test's subject:
    without it every other schedule test stays green while a station started
    from the command line packs nothing. The station itself is replaced,
    because a real one ticks over the operator's own crew home.
    """

    from reckon.crew import dispatch_launch, station, station_store
    from reckon.crew import repository_maintenance as maintenance

    built: list[_Station] = []
    adopted: list[str] = []

    class _Recording(_Station):
        def __init__(self, store, **kwargs) -> None:
            super().__init__(store, **kwargs)
            built.append(self)

    monkeypatch.setattr(station, "Station", _Recording)
    monkeypatch.setattr(
        dispatch_launch,
        "_adopt_launched_workers_from_reexec",
        lambda: adopted.append("adopted"),
    )

    def runtime_root() -> station_store.StationStore:
        return station_store.StationStore(tmp_path / "runtime")

    monkeypatch.setattr(
        station_store.StationStore, "at_runtime_root", staticmethod(runtime_root)
    )

    station.run_station(once=True)

    assert adopted == ["adopted"], adopted
    assert len(built) == 1, built
    active = built[0]
    assert isinstance(active.maintenance, maintenance.MaintenanceSweep), (
        active.maintenance
    )
    # The sweep is asked on the tick, which is the whole point of wiring it.
    assert active.ticked == 1


def test_a_refused_submission_is_reported_and_does_not_stop_the_tick(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One repository's refused job leaves the other's pass submitted."""

    from reckon.crew import repository_maintenance as maintenance

    first = _repository(tmp_path, "first")
    second = _repository(tmp_path, "second")
    submitted: list[str] = []

    def submit(path: Path) -> dict:
        if path.name == "first":
            raise OSError("sbatch refused the maintenance pass: invalid partition")
        submitted.append(str(path))
        return {"repository": str(path), "job_id": "1"}

    sweep = maintenance.MaintenanceSweep(home=home, clock=lambda: MOMENT, submit=submit)
    node = _station(
        tmp_path, home, {"first": first / "docs", "second": second / "docs"}, sweep
    )

    node.tick(now=MOMENT)

    assert submitted == [str(second)]
    lines = [
        line
        # The row is printed, so a coordinator reading the station's stream
        # learns a repository went unpacked without reading the scheduler.
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("station-maintenance:")
    ]
    assert any("'ok': False" in line and str(first) in line for line in lines), lines
