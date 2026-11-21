"""A repeated run-store query decodes nothing while the store stands still."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from reckon import file_memo, run_store

PROJECTS = ("alpha", "beta", "gamma")
ALPHA_ROWS = 800
OTHER_ROWS = (700, 500)


def _row(project: str, number: int) -> dict[str, Any]:
    return {
        "run_id": _run_id(project, number),
        "project": project,
        "node": f"node-{number % 4}",
        "plan": f"plan-{number % 3}",
        "outcome": f"finished {number}",
        "activity_seconds": float(number),
    }


def _run_id(project: str, number: int) -> str:
    return f"{project}-{number:05d}"


@pytest.fixture(scope="module")
def population(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("run-store") / "runs.db"
    with run_store.RunStore(path) as handle:
        for project, count in zip(PROJECTS, (ALPHA_ROWS, *OTHER_ROWS), strict=True):
            for number in range(count):
                handle.append(project, _row(project, number))
    return path


def _copy(source: Path, directory: Path) -> Path:
    copy = directory / "runs.db"
    copy.parent.mkdir()
    shutil.copyfile(source, copy)
    return copy


@pytest.fixture
def store(tmp_path, population: Path) -> Path:
    """Each test reads its own copy, so no answer outlives the test that made it."""
    return _copy(population, tmp_path / "store")


@pytest.fixture
def cold(tmp_path, population: Path) -> Path:
    """A second copy of the same rows, read with no answer to reuse."""
    return _copy(population, tmp_path / "cold")


class _RowDecodes:
    """A stand-in for the JSON decoder that counts decoded run rows."""

    def __init__(self, real):
        self._real = real
        self.rows = 0

    def __call__(self, text, *args, **kwargs):
        value = self._real(text, *args, **kwargs)
        if isinstance(value, dict) and "run_id" in value:
            self.rows += 1
        return value


@pytest.fixture
def decodes(monkeypatch) -> _RowDecodes:
    counter = _RowDecodes(run_store.json.loads)
    monkeypatch.setattr(run_store.json, "loads", counter)
    return counter


def _read(project: str, path: Path, **kwargs):
    return run_store.stored_run_rows(project, path=path, **kwargs)


def test_the_fixture_holds_the_declared_population(store: Path) -> None:
    assert len(_read("alpha", store)) == ALPHA_ROWS
    assert len(_read("beta", store)) == OTHER_ROWS[0]
    assert len(_read("gamma", store)) == OTHER_ROWS[1]


def test_four_identical_calls_decode_the_rows_once(store: Path, decodes) -> None:
    answers = [_read("alpha", store) for _ in range(4)]

    assert decodes.rows == ALPHA_ROWS
    for answer in answers:
        assert answer == answers[0]
        assert len(answer) == ALPHA_ROWS


def test_each_argument_answers_with_the_uncached_answer(
    store: Path, cold: Path, decodes
) -> None:
    base = _read("alpha", store)
    assert decodes.rows == ALPHA_ROWS

    variants = (
        ("other project", lambda path: _read("beta", path), OTHER_ROWS[0]),
        ("one run id", lambda path: _read("alpha", path, run_ids=["alpha-00007"]), 1),
        (
            "run_id field",
            lambda path: _read("alpha", path, fields=("run_id",)),
            ALPHA_ROWS,
        ),
        ("node filter", lambda path: _read("alpha", path, node="node-3"), 200),
        ("plan filter", lambda path: _read("alpha", path, plan="plan-1"), 267),
    )

    for label, call, expected_length in variants:
        from_cache = call(store)
        assert len(from_cache) == expected_length, label
        # The cold copy carries the same rows and no answer to reuse, so its
        # read is the answer the base revision gives for these arguments.
        assert from_cache == call(cold), label
        assert from_cache != base, label


def test_an_updated_row_is_decoded_again(store: Path, decodes) -> None:
    before = _read("alpha", store)
    assert decodes.rows == ALPHA_ROWS

    updated = dict(before[0])
    updated["outcome"] = "amended after the fact"
    with run_store.RunStore(store) as handle:
        assert handle.sync_run("alpha", updated) == "updated"

    before_second_read = decodes.rows
    after = _read("alpha", store)

    assert decodes.rows - before_second_read == ALPHA_ROWS
    assert len(after) == len(before)
    assert after[0] == updated
    assert after[1:] == before[1:]


def test_mutating_a_returned_record_leaves_the_next_answer_alone(
    store: Path, decodes
) -> None:
    first = _read("alpha", store)
    pristine = first[0]["outcome"]
    first[0]["outcome"] = "mutated by the caller"
    assert decodes.rows == ALPHA_ROWS

    second = _read("alpha", store)

    assert decodes.rows == ALPHA_ROWS
    assert second[0]["outcome"] == pristine


def test_clearing_the_memo_makes_the_next_read_decode_again(
    store: Path, decodes
) -> None:
    """The repository's cold-read instrument reaches this answer too."""
    _read("alpha", store)
    assert decodes.rows == ALPHA_ROWS

    file_memo.clear()

    _read("alpha", store)
    assert decodes.rows == 2 * ALPHA_ROWS
