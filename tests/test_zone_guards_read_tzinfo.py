"""The zone guards read the parsed tzinfo, not the stamp's text shape.

Three defects recorded at base ``92274925`` by running the base code:

* ``reckon/crew/ticker.py:row_moment`` and the ``reckon/_backends.py`` cache
  stamp reader each decided whether a stamp named a zone from a regex over the
  text's tail. That regex required four or six digits in the offset, so the
  offset spellings ``datetime.fromisoformat`` accepts — ``+00``, ``-05``,
  ``+000000`` — read as naming no zone. Under ``TZ=Europe/Paris`` with the
  summer stamp ``2026-07-01T00:00:00-05``, ``row_moment`` then read the stamp
  as a local wall clock and returned ``1782874800`` — a ``7200`` s shift from
  the instant the offset names — and the cache reader returned ``None``, an
  unstated age for a stamp that did state its zone.
* the shared writer's sibling temporary was removed when the ``chmod`` raised
  but not when the ``os.fdopen`` after it raised, so a failed open left the
  file ``os.open`` had created on disk.

The head reads the zone from :func:`reckon._timestamps.parse_iso`'s ``tzinfo``
and removes every failure's sibling, so each offset spelling returns the aware
instant the pre-migration base ``cea6da07`` returned and a failed open leaves no
file. The settings writer holds a non-ASCII hook-script path literally instead
of escaped, its bytes as they were before the second writer wave.
"""

from __future__ import annotations

import io
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest

from reckon import _store
from reckon._backends import ACCOUNT_CACHE_STAMP, _parse_cached_fetch_stamp
from reckon._store import write_json_atomically
from reckon._timestamps import parse_utc
from reckon.crew.ticker import row_moment
from reckon.hooks.install import install_hook_settings

# A summer stamp, so Europe/Paris carries its own ``+02:00`` offset and the
# misread the base produced is the full two hours the shift statement names.
PARIS_OFFSET_SPELLINGS = (
    "2026-07-01T00:00:00+00",
    "2026-07-01T00:00:00-05",
    "2026-07-01T00:00:00+000000",
)

# The text-shape guard the base used, kept here only to reproduce its defect.
_BASE_NAMED_ZONE = re.compile(r"(?:Z|[+-]\d{2}:?\d{2}(?::\d{2})?)$")


@pytest.fixture
def paris_clock(monkeypatch: pytest.MonkeyPatch) -> object:
    """Run a test under ``TZ=Europe/Paris`` and restore the process zone."""
    original = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Europe/Paris")
    time.tzset()
    try:
        yield
    finally:
        if original is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", original)
        time.tzset()


def _base_row_moment(text: str) -> float:
    """The base ``row_moment`` body, reproduced so its defect can be shown."""
    if text != text.strip() or text.endswith("z"):
        return 0.0
    moment = parse_utc(text)
    if moment is None:
        return 0.0
    if _BASE_NAMED_ZONE.search(text):
        return moment.timestamp()
    return moment.replace(tzinfo=None).astimezone().timestamp()


def _reference_moment(text: str) -> float:
    """The instant the pre-migration base ``cea6da07`` returned for a stamp."""
    return datetime.fromisoformat(text).timestamp()


def test_row_moment_reads_a_stamp_zone_from_tzinfo(paris_clock: object) -> None:
    """Every accepted offset spelling returns the aware instant it names."""
    shift_for_minus_05 = None
    for spelling in PARIS_OFFSET_SPELLINGS:
        expected = _reference_moment(spelling)
        got = row_moment({"observed_at": spelling})
        assert got == expected, f"{spelling}: {got} != aware instant {expected}"
        assert got != _base_row_moment(spelling), f"{spelling}: base defect not fixed"
        if spelling.endswith("-05"):
            shift_for_minus_05 = expected - _base_row_moment(spelling)
    assert shift_for_minus_05 == 7200.0, shift_for_minus_05


def test_row_moment_still_reads_a_zoneless_stamp_as_local(paris_clock: object) -> None:
    """The change moved only zone detection: a zoneless stamp stays local."""
    naive = "2026-07-01T00:00:00"
    assert row_moment({"observed_at": naive}) == _base_row_moment(naive)
    assert row_moment({"observed_at": naive}) == _reference_moment(naive)


def test_cache_stamp_reader_reads_a_stamp_zone_from_tzinfo(
    paris_clock: object,
) -> None:
    """Each accepted offset spelling is a trusted, zoneless is refused."""
    for spelling in PARIS_OFFSET_SPELLINGS:
        payload = {ACCOUNT_CACHE_STAMP: spelling}
        moment = _parse_cached_fetch_stamp(payload)
        assert moment is not None, f"{spelling}: base refused a stated zone"
        assert moment.tzinfo is not None
        assert moment == datetime.fromisoformat(spelling).astimezone(UTC)
        # The base text guard refused all three spellings.
        assert _BASE_NAMED_ZONE.search(spelling) is None
    assert (
        _parse_cached_fetch_stamp({ACCOUNT_CACHE_STAMP: "2026-07-01T00:00:00"}) is None
    )


def test_settings_writer_keeps_a_non_ascii_script_path_literal(tmp_path: Path) -> None:
    """A non-ASCII hook-script path lands in the settings file unescaped."""
    settings = tmp_path / "settings.json"
    script = tmp_path / "häké" / "hook.py"
    install_hook_settings(
        settings, write=True, script_path=script, stream=io.StringIO()
    )
    raw = settings.read_bytes()
    assert "häké".encode() in raw, raw
    assert b"h\\u00e4k" not in raw, raw


def test_fdopen_failure_leaves_no_temporary_beside_the_destination(
    tmp_path: Path,
) -> None:
    """A failing os.fdopen removes the sibling os.open created."""
    destination = tmp_path / "dest.json"
    destination.write_text("OLD\n")

    def refuse_fdopen(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated fdopen failure")

    with mock.patch.object(_store.os, "fdopen", refuse_fdopen):
        try:
            write_json_atomically(destination, {"a": 1})
        except OSError:
            pass
        else:  # pragma: no cover - the refusal must propagate
            raise AssertionError("the fdopen failure did not propagate")

    assert destination.read_text() == "OLD\n"
    leftover = sorted(p.name for p in tmp_path.iterdir() if p.name != "dest.json")
    assert leftover == []
