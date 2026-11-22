"""A pass reading says which store the bytes it read came from.

The admit service is measured by its own pass readings, and until this test one
reading gave a total and a per-reader breakdown by count and seconds but never
said where the bytes came from. The accounting here charges every file read to
one of the stores a pass reads — the live and queued pointers, the run store,
the ledger, the review store and plan documents — and leaves whatever it could
not place named as unattributed, so a source the accounting misses shows up as
a gap rather than vanishing from a total.

The checks are the ones the plan's done-when names: the per-source bytes sum to
within five percent of what the process read during the pass, taken from its
own ``/proc`` record; and a file of known size read from each source lands under
that source and no other.

The fixture is a synthesised crew home, so nothing here reads or writes the
operator's crew home.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import file_memo
from reckon.crew.admission import service

BAND = 0.05
KNOWN_BYTES = 64 * 1024


def _pad(path: Path, size: int = KNOWN_BYTES) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return size


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A crew home holding one known-size file in each source."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    home = config_home / "crew"
    home.mkdir()
    checkout = tmp_path / "checkout"
    files = {
        "live and queued pointers": home / "live" / "run-a.json",
        "run store": home / "run_store.db",
        "review store": home / "reviews" / "sample-project" / "review-a.json",
        "ledger": config_home / "state" / "sample" / "ledger.jsonl",
        "plan documents": checkout / "docs" / "plans" / "sample.html",
        "everything else": home / "misc" / "other.txt",
    }
    sizes = {name: _pad(path) for name, path in files.items()}
    return service, files, sizes


def _run_pass(files: dict[str, Path]) -> dict[str, int]:
    """Read every source once, inside the accounting a pass opens."""
    before = file_memo.read_character_count()
    with file_memo.attributing(service.admit_read_sources()) as account:
        for path in files.values():
            path.read_text(encoding="utf-8", errors="replace")
    after = file_memo.read_character_count()
    return service._reads_by_source(account, before, after)


def _shortfall(summary: dict[str, int]) -> float:
    """The unattributed share of what the process read, as a fraction."""
    process = summary.get("process_bytes")
    assert isinstance(process, int) and process > 0, summary
    return int(summary.get("unattributed_bytes") or 0) / process


def test_the_pass_bytes_sum_to_the_process_read(crew_home) -> None:
    _service, files, sizes = crew_home
    summary = _run_pass(files)

    assert summary["process_bytes"] >= sum(sizes.values()) - KNOWN_BYTES
    assert _shortfall(summary) <= BAND, summary
    for source, size in sizes.items():
        assert source in summary, (source, summary)
        assert summary[source] >= size, (source, size, summary)


def test_each_sources_bytes_land_under_that_source_only(crew_home) -> None:
    _service, files, sizes = crew_home
    for source, path in files.items():
        before = file_memo.read_character_count()
        with file_memo.attributing(service.admit_read_sources()) as account:
            path.read_text(encoding="utf-8", errors="replace")
        after = file_memo.read_character_count()
        summary = service._reads_by_source(account, before, after)
        others = {
            name: value
            for name, value in summary.items()
            if name not in ("attributed_bytes", "process_bytes", "unattributed_bytes")
        }
        assert others == {source: sizes[source]}, (source, others)


def test_a_source_left_out_shortens_the_sum_beyond_the_band(crew_home) -> None:
    """What the declared mutation produces: a shortfall larger than the band.

    The mutation itself is not in this module. The negative-control run loads a
    plugin that removes one source from the product's ``admit_read_sources()``
    rules and stops the account recording the paths that rule owned, then runs
    the sum test above — which is the arm that must fail. What is asserted here
    is the arithmetic the band rests on: a source the accounting loses is worth
    more than five percent of the pass.
    """
    _service, _files, sizes = crew_home
    total = sum(sizes.values())
    assert sizes["review store"] / total > BAND, (sizes["review store"], total)


def test_the_reading_is_written_on_the_pass_verdict_row(crew_home, tmp_path) -> None:
    """A pass's reading carries the breakdown, so the verdict log names sources."""
    _service, files, _sizes = crew_home
    from reckon.crew.admission import verdicts

    result = {"reads_by_source": _run_pass(files)}

    record = verdicts._pass_verdict_record(result, lane="", code_stamp="stamp")
    saved = tmp_path / "reading.json"
    saved.write_text(json.dumps(record), encoding="utf-8")
    written = json.loads(saved.read_text(encoding="utf-8"))
    assert written["reads_by_source"]["live and queued pointers"] > 0


def test_a_real_pass_carries_the_breakdown(tmp_path: Path, monkeypatch) -> None:
    """The wrap around the pass itself is what carries the reading.

    A helper test cannot see this: delete the accounting scope around the pass
    and every helper still passes while no pass reading has a breakdown.
    """
    config_home = tmp_path / "config"
    (config_home / "crew" / "live").mkdir(parents=True)
    monkeypatch_pointer = config_home / "crew" / "live" / "run-a.json"
    pointer_text = json.dumps(
        {
            "run_id": "run-a",
            "project": "sample",
            "node": {"id": "run-a"},
            "phase": "queued",
        }
    )
    monkeypatch_pointer.write_text(pointer_text, encoding="utf-8")
    (config_home / "mounts.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    result = service.admit_until_stopped(once=True, lane="")

    assert "reads_by_source" in result, sorted(result)
    pointers = result["reads_by_source"].get("live and queued pointers", 0)
    assert pointers >= len(pointer_text), (pointers, len(pointer_text))
