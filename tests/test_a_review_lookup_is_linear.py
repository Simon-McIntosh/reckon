"""A review lookup finds every review in flight without a per-candidate sweep.

The station classifies every live pointer once a tick, and a delivered run that
owes a review asks the reflex's own recognition which live reviewer stands for
the run's head. That recognition walked the live pointers and read each
candidate's reviewed run directory inside the walk — the reviewed run is the
same on every iteration of one row — so a tick over P pointers paid on the
order of P² filesystem checks and never finished on a fleet a restart had left
full of dead runs' pointers.

The fixture is a temporary crew home of 200 live pointers: 160 delivered
implement runs whose manifest reports completion and which carry no review, so
each reaches the scoring branch, and 40 live reviews declaring the pairing
those runs' heads compose. A recording stand-in counts every ``Path.exists``
and ``Path.is_dir`` call made under the crew home while the lookup looks a
review in flight, and the first case asserts that count stays within five
checks for each live pointer. The review matched to each run is compared with
the pairing the base read path returns — the same walk performed one candidate
at a time — so answering those reads once can only change how often the lookup
asks, never what it answers. A second case doubles the fleet and asserts the
count at most doubles.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Self

import pytest

from reckon.crew import recovery_review_dispatch as dispatch
from reckon.crew import review as review_module
from reckon.crew import runs
from reckon.crew.recovery_classification import classify_pointer

PROJECT = "review-lookup-fixture"
SESSION = "session-review-lookup"
DELIVERED_RUNS = 160
REVIEWED_RUNS = 40
LIVE_POINTERS = DELIVERED_RUNS + REVIEWED_RUNS
CHECK_BUDGET_PER_POINTER = 5

_MATCHED_REVIEW_RE = re.compile(r"in flight as (?P<review>[^\s;]+)")


def _head(index: int) -> str:
    """A distinct 40-character revision for the run at ``index``."""
    return f"{index + 1:040x}"


def _crew_home(tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary configuration home; the crew home is its ``crew`` child."""
    home = tmp_path / name
    (home / "crew").mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    return home


def _delivered_run(home: Path, index: int) -> dict[str, Any]:
    """One delivered implement run that owes a review of its own head."""
    run_id = f"run-{index:03d}"
    head = _head(index)
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: [{head}]\n",
        encoding="utf-8",
    )
    pointer: dict[str, Any] = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "role": "implement",
        "process_alive": False,
        "launcher_host": "another-host",
        "base_sha": head,
        "manifest_path": str(manifest),
        "node": {
            "id": run_id,
            "plan": "fixture-plan",
            "section": "fixture-section",
            "write_paths": ["fixture-module.py"],
        },
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _live_review(reviewed: dict[str, Any]) -> dict[str, Any]:
    """A live review declaring the head-keyed record path of ``reviewed``."""
    run_id = str(reviewed["run_id"])
    review_id = f"review-{run_id[len('run-') :]}"
    pointer: dict[str, Any] = {
        "run_id": review_id,
        "project": PROJECT,
        "session": SESSION,
        "role": "review",
        "phase": "working",
        "process_alive": False,
        "launcher_host": "another-host",
        "node": {
            "id": f"review-of-{run_id}",
            "plan": "fixture-plan",
            "section": "fixture-section",
            "write_paths": review_module.run_review_record_paths(
                runs.run_dir(run_id),
                run_id,
                reviewed_head_sha=str(reviewed["base_sha"]),
            ),
        },
    }
    runs._write_json(runs.pointer_path(review_id), pointer)
    return pointer


def _fleet(home: Path, *, scale: int = 1) -> list[dict[str, Any]]:
    """A live-pointer read of ``scale`` copies of the fixture's population."""
    delivered = DELIVERED_RUNS * scale
    reviewed = REVIEWED_RUNS * scale
    for index in range(delivered):
        pointer = _delivered_run(home, index)
        if index < reviewed:
            _live_review(pointer)
    pointers = runs.list_live(project=PROJECT)
    assert len(pointers) == delivered + reviewed, "the fixture's fleet moved"
    return pointers


class _CheckCounter:
    """Count the existence checks the review lookup makes under the crew home.

    The stand-in wraps ``Path.exists`` and ``Path.is_dir`` and records a call
    only while the lookup's per-candidate readers are running, which is the
    sweep this measure is about: the walk one classified run makes over the
    live pointers. The checks the rest of one classification makes belong to
    other readers, so counting them would let this case pass or fail on
    machinery the lookup neither owns nor runs.
    """

    def __init__(self, home: Path) -> None:
        self._home = str(home)
        self._depth = 0
        self.checks = 0
        self._real_exists = Path.exists
        self._real_is_dir = Path.is_dir

    def __enter__(self) -> Self:
        counter = self

        def exists(path: Path, *arguments: Any, **keywords: Any) -> bool:
            counter._record(path)
            return counter._real_exists(path, *arguments, **keywords)

        def is_dir(path: Path, *arguments: Any, **keywords: Any) -> bool:
            counter._record(path)
            return counter._real_is_dir(path, *arguments, **keywords)

        Path.exists = exists
        Path.is_dir = is_dir
        return self

    def __exit__(self, *arguments: object) -> bool:
        Path.exists = self._real_exists
        Path.is_dir = self._real_is_dir
        return False

    def _record(self, path: Path) -> None:
        if self._depth and str(path).startswith(self._home):
            self.checks += 1

    @contextmanager
    def armed(self) -> Iterator[None]:
        """Count the checks made inside this block."""
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1


@contextmanager
def _counted_lookup(
    monkeypatch: pytest.MonkeyPatch, counter: _CheckCounter
) -> Iterator[Iterator[None]]:
    """Arm the counter while a per-candidate reader of the lookup runs."""
    for name in ("_review_pointer_rubric", "_review_subject"):
        real = getattr(dispatch, name)

        def counting(*arguments: Any, _real: Any = real, **keywords: Any) -> Any:
            with counter.armed():
                return _real(*arguments, **keywords)

        monkeypatch.setattr(dispatch, name, counting)
    yield


def _classify_every_pointer(
    pointers: list[dict[str, Any]],
) -> tuple[dict[str, str], int]:
    """Classify each pointer once; return the review each delivered run matched.

    The row's own sentence is read for the review it names, because the review
    a scoring run is matched to is what the row tells a coordinator, not an
    internal value this case could read around it.
    """
    matched: dict[str, str] = {}
    scoring = 0
    for pointer in pointers:
        row = classify_pointer(pointer, pointers=pointers)
        if row.get("classification") != "scoring":
            continue
        scoring += 1
        found = _MATCHED_REVIEW_RE.search(str(row.get("detail") or ""))
        matched[str(pointer["run_id"])] = found.group("review") if found else ""
    return matched, scoring


def _base_pointer_rubric(
    pointer: dict[str, Any], reviewed_run_id: str, reviewed_head: str
) -> str | None:
    """The rubric a pointer's record paths encode, read one run at a time."""
    node = pointer.get("node") or {}
    declared = [Path(str(path)) for path in node.get("write_paths") or ()]
    if not reviewed_run_id or not declared:
        return None
    reviewed_directory = review_module.run_review_record_directory(
        runs.run_dir(reviewed_run_id)
    )
    inside = {path.name for path in declared if path.parent == reviewed_directory}
    if not inside:
        return None
    for candidate in (review_module.FIGURE_REVIEW_RUBRIC, None):
        composed = [
            Path(path).name
            for path in review_module.run_review_record_paths(
                runs.run_dir(reviewed_run_id),
                reviewed_run_id,
                reviewed_head_sha=reviewed_head,
                rubric=candidate,
            )
        ]
        if composed[-1] in inside:
            return candidate or ""
    return None


def _base_review_in_flight(
    record: dict[str, Any], pointers: list[dict[str, Any]]
) -> str:
    """The base read path: the walk as it stood before the snapshot answered it.

    Every candidate's reviewed run directory is read again here, which is the
    cost this node removed, so the pairing this returns is the answer that cost
    produced rather than a second implementation of the rule.
    """
    fields = dispatch._review_dispatch_fields(record, rubric=None)
    head = str(fields.get("head") or "")
    recorded = record.get(dispatch._review_dispatch_field(None))
    if isinstance(recorded, dict) and str(recorded.get("status") or "") == "withdrawn":
        recorded = None
    if isinstance(recorded, dict):
        standing = str(recorded.get("run_id") or "")
        if (
            standing
            and dispatch._review_head_covers(str(recorded.get("head") or ""), head)
            and dispatch._recorded_review_is_live(recorded)
        ):
            return standing
    project = str(fields["project"] or "")
    if not project:
        return ""
    run_id = str(record.get("run_id") or "")
    for pointer in pointers:
        rubric = _base_pointer_rubric(pointer, run_id, head)
        if rubric is not None:
            if rubric:
                continue
            return str(pointer.get("run_id") or "")
        if not dispatch._is_review_run(pointer):
            continue
        reviewed, reviewed_head = dispatch._review_subject(
            pointer, project, pointers=pointers
        )
        if (
            reviewed
            and reviewed == run_id
            and dispatch._review_head_covers(reviewed_head, head)
        ):
            return str(pointer.get("run_id") or "")
    return ""


def _base_pairing(pointers: list[dict[str, Any]]) -> dict[str, str]:
    """The base read path's answer for every delivered run in the fleet."""
    return {
        str(pointer["run_id"]): _base_review_in_flight(pointer, pointers)
        for pointer in pointers
        if str(pointer.get("role") or "") != "review"
    }


def _measure_checks(
    home: Path, monkeypatch: pytest.MonkeyPatch, *, scale: int = 1
) -> tuple[int, int]:
    """Classify one fleet once, returning the sweep's checks and its size."""
    pointers = _fleet(home, scale=scale)
    counter = _CheckCounter(home)
    with counter, _counted_lookup(monkeypatch, counter):
        _classify_every_pointer(pointers)
    return counter.checks, len(pointers)


def test_a_review_lookup_checks_each_pointer_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _crew_home(tmp_path, "single", monkeypatch)
    pointers = _fleet(home)
    assert len(pointers) == LIVE_POINTERS, "the fixture's fleet moved"
    counter = _CheckCounter(home)
    with counter, _counted_lookup(monkeypatch, counter):
        matched, scoring = _classify_every_pointer(pointers)
    per_pointer = counter.checks / len(pointers)
    print(
        f"checks={counter.checks} pointers={len(pointers)} "
        f"per_pointer={per_pointer:.2f} scoring={scoring}"
    )

    assert scoring == DELIVERED_RUNS, "the fixture must reach the scoring branch"
    assert set(matched) == {f"run-{index:03d}" for index in range(DELIVERED_RUNS)}
    assert matched == _base_pairing(pointers), "the lookup changed the pairing"
    assert matched == {
        f"run-{index:03d}": (f"review-{index:03d}" if index < REVIEWED_RUNS else "")
        for index in range(DELIVERED_RUNS)
    }, "the fixture must match each run to the review declared for its head"

    # A zero count would mean the stand-in saw nothing, which proves nothing.
    assert counter.checks, "the instrument must observe the sweep it counts"
    assert counter.checks <= CHECK_BUDGET_PER_POINTER * len(pointers), (
        f"the lookup made {counter.checks} checks for {len(pointers)} live "
        f"pointers ({per_pointer:.2f} each): it reads per candidate, not once"
    )


def test_a_doubled_fleet_at_most_doubles_the_lookups_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    single_home = _crew_home(tmp_path, "single", monkeypatch)
    single_checks, single_pointers = _measure_checks(single_home, monkeypatch)
    double_home = _crew_home(tmp_path, "double", monkeypatch)
    double_checks, double_pointers = _measure_checks(double_home, monkeypatch, scale=2)
    print(
        f"single={single_checks} for {single_pointers} pointers, "
        f"double={double_checks} for {double_pointers} pointers"
    )
    assert single_checks, "the instrument must observe the sweep it counts"
    assert double_pointers == 2 * single_pointers, "the doubling did not happen"
    assert double_checks <= 2 * single_checks, (
        f"doubling the fleet took the lookup from {single_checks} checks to "
        f"{double_checks}: the count is not linear in the live pointers"
    )
