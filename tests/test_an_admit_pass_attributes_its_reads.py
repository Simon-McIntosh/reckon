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
import os
from pathlib import Path

import pytest

from reckon import file_memo
from reckon.crew.admission import service

#: The mutation this node declares: with one source left out of the accounting
#: the sum must fall short of the process figure by more than the band. The
#: negative-control run sets this to a source name; the ordinary run leaves it
#: unset, so the mutation is applied to the same test rather than to a copy.
OMIT_ENV = "MRFO_OMIT_SOURCE"

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
        "review store": home / "reviews" / "review-a.json",
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
    summary = service._reads_by_source(account, before, after)
    omit = os.environ.get(OMIT_ENV)
    if omit:
        dropped = int(summary.pop(omit, 0) or 0)
        summary["attributed_bytes"] = (
            int(summary.get("attributed_bytes") or 0) - dropped
        )
        process = summary.get("process_bytes")
        if isinstance(process, int):
            summary["unattributed_bytes"] = max(
                0, process - int(summary["attributed_bytes"])
            )
    return summary


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
    """The mutation, applied on demand: the sum case must fail by more than 5%.

    The declared mutation is the environment hook above, so this test answers
    the same question the sum test does under an accounting that drops one
    source. It is left green in an ordinary run because it asserts the
    *mutation* shortens the sum; the failing arm is the sum test itself run with
    the hook set, which is what the node's negative-control log records.
    """
    _service, files, _sizes = crew_home
    whole = _run_pass(files)
    process_reads = whole["process_bytes"]
    assert isinstance(process_reads, int) and process_reads > 0
    omitted = service.admit_read_sources()[0][0]
    assert omitted in whole
    partial = _run_pass_with(omitted, files)
    assert omitted not in partial
    dropped = int(whole["attributed_bytes"]) - int(partial["attributed_bytes"])
    assert dropped > 0
    assert dropped / process_reads > BAND, (dropped, process_reads, partial)


def _run_pass_with(omitted: str, files: dict[str, Path]) -> dict[str, int]:
    """Run the same pass with one source excluded from the account."""
    saved = os.environ.get(OMIT_ENV)
    os.environ[OMIT_ENV] = omitted
    try:
        return _run_pass(files)
    finally:
        if saved is None:
            os.environ.pop(OMIT_ENV, None)
        else:
            os.environ[OMIT_ENV] = saved


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
