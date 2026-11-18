"""The obligation sweep's review question is asked once per live pointer.

An obligation build asks, for every delivered run, which live reviewer already
stands for its head. The rule that answers it walks the live pointers, the build
asks it once per row, three separate readers ask it again, and the pointer set is
re-read between them — so on a fleet a restart has left full of pointers the
sweep paid a walk per run, and every walk resolved the reviewed run's record
directory and each candidate's subject from the filesystem. Measured on this
fixture at the revision before this change: 20,920 ``Path.exists`` and
``Path.is_dir`` calls made inside the lookups alone.

The rule now files each live pointer once, under the record directories it was
granted and the run its declarations name, and answers a run's question from the
short list its own identity selects, through the index of that pointer read.
This case runs one obligation build over the fixture and asserts two things: the
existence checks made inside the lookups stay within five per live pointer, and
the review each run is matched to is the one the base read path returns for the
same pointers.

The base path is spelled out below as the walk it was — every candidate examined,
each candidate's reviewed run directory read again — so the pairing compared is
the answer that cost produced rather than a second implementation of the rule.

The recording stand-in wraps ``Path.exists`` and ``Path.is_dir`` and counts only
the calls made while one of the three review-lookup entry points is running,
which is the seam this measure is about: the build's other readers — the
classifier's own reads, the flight configuration, the ledger — are their own
cost, and counting them would let this case pass or fail on machinery the lookup
neither owns nor runs.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Self

import pytest

from reckon.crew import obligations, runs
from reckon.crew import recovery_classification as classification
from reckon.crew import recovery_review_dispatch as dispatch
from reckon.crew import review as review_module

PROJECT = "obligation-review-index"
SESSION = "session-obligation-review-index"
DELIVERED_RUNS = 160
REVIEWED_RUNS = 40
LIVE_POINTERS = DELIVERED_RUNS + REVIEWED_RUNS
CHECK_BUDGET_PER_POINTER = 5

_MATCHED_REVIEW_RE = re.compile(r"in flight as (?P<review>[^\s;]+)")


def _head(index: int) -> str:
    """A distinct 40-character revision for the run at ``index``."""
    return f"{index + 1:040x}"


def _crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary configuration home; the crew home is its ``crew`` child."""
    home = tmp_path / "config"
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


def _fleet(home: Path) -> list[dict[str, Any]]:
    """The fixture's fleet: delivered runs, the first of them reviewed."""
    for index in range(DELIVERED_RUNS):
        pointer = _delivered_run(home, index)
        if index < REVIEWED_RUNS:
            _live_review(pointer)
    pointers = runs.list_live(project=PROJECT)
    assert len(pointers) == LIVE_POINTERS, "the fixture's fleet moved"
    return pointers


class _CheckCounter:
    """Count existence checks made while the review lookup is running.

    The stand-in wraps ``Path.exists`` and ``Path.is_dir`` and records a call
    only while one of the lookups the sweep enters is on the stack. Nested
    entries are counted once, because the inner lookup is the same work.
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
def _counted_lookups(
    monkeypatch: pytest.MonkeyPatch, counter: _CheckCounter
) -> Iterator[None]:
    """Arm the counter around every entry point into the review lookup."""
    seam = (
        (obligations, "_in_flight_review"),
        (classification, "_review_run_in_flight"),
        (runs, "_in_flight_round_ids"),
    )
    for module, name in seam:
        real = getattr(module, name)

        def counting(*arguments: Any, _real: Any = real, **keywords: Any) -> Any:
            with counter.armed():
                return _real(*arguments, **keywords)

        monkeypatch.setattr(module, name, counting)
    yield


def _matched_reviews(view: dict[str, Any]) -> dict[str, str]:
    """The review each obligation row names as in flight, by run id.

    A row's own sentence is read rather than any internal value, because the
    review a run is matched to is what the row tells a coordinator.
    """
    matched: dict[str, str] = {}
    for row in view.get("obligations") or ():
        if not isinstance(row, dict):
            continue
        found = _MATCHED_REVIEW_RE.search(str(row.get("next_command") or ""))
        matched[str(row.get("run_id") or "")] = found.group("review") if found else ""
    return matched


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
    """The base read path: the walk as it stood before the index answered it.

    Every candidate is examined and the reviewed run's directory is read again
    inside the walk, which is the cost this measure is about, so the pairing this
    returns is the answer that cost produced.
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


def _sweep(home: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], int]:
    """Run one obligation build; return its view and the checks it made."""
    _fleet(home)
    counter = _CheckCounter(home)
    with counter, _counted_lookups(monkeypatch, counter):
        view = obligations.obligations(PROJECT, SESSION)
    return view, counter.checks


def test_the_obligation_sweep_checks_each_pointer_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _crew_home(tmp_path, monkeypatch)
    view, checks = _sweep(home, monkeypatch)

    matched = _matched_reviews(view)
    assert len(matched) == DELIVERED_RUNS, (
        f"the sweep drew {len(matched)} rows, not {DELIVERED_RUNS}"
    )
    # Not a vacuous pass: the fixture's reviews must be the code path taken.
    assert sum(1 for review in matched.values() if review) == REVIEWED_RUNS
    assert checks <= CHECK_BUDGET_PER_POINTER * LIVE_POINTERS, (
        f"the lookup made {checks} existence checks over {LIVE_POINTERS} pointers"
    )

    pointers = runs.list_live(project=PROJECT)
    assert matched == _base_pairing(pointers)
