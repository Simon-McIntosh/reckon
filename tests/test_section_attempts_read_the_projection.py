"""A section-attempts read decodes only the index's narrow projection.

``section_attempts_by_plan`` used to take its history from :func:`ledger.load`,
which decodes every retained payload in the ledger index on every warm call —
seconds of ``json.loads`` over thousands of records, against a read deadline it
must answer inside. The index already stores a projection beside every payload
and ``ledger.picker_runs`` reads it, so the aggregate reads that instead.

Three things hold the change in place: the projection and the full rows agree
for the fields the grouping reads, a call never asks the index for a full
payload, and an index built by an earlier projection version is rebuilt on the
first read rather than served with the missing fields.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from reckon import ledger, mcp_views

PROJECT = "projection-fixture"
PLAN = "a-read-answers-inside-its-deadline"
FRAGMENT = (
    Path(__file__).resolve().parents[1]
    / "docs/evidence/fragments/a-read-answers-inside-its-deadline"
    / "a-section-attempts-read-the-projection.html"
)


def _row(
    index: int,
    *,
    role: str = "implement",
    plan: str = PLAN,
    section: str = "s45",
    gate: str = "passed",
    failure_classification: str | None = None,
) -> dict[str, object]:
    run_id = f"r-20261017T0000{index:02d}-row-{index:02d}"
    row: dict[str, object] = {
        "run_id": run_id,
        "project": PROJECT,
        "node": f"node-{index}",
        "role": role,
        "plan": plan,
        "section": section,
        "gate": gate,
    }
    if failure_classification is not None:
        row["failure_classification"] = failure_classification
    return row


# The fixture carries every shape the grouping discriminates on: a promoted
# row, a failed row whose classification the result must keep, a settled row
# from a non-executable role, and a row belonging to another section.
ROWS = [
    _row(0),
    _row(1, gate="failed", failure_classification="test_failure"),
    _row(2, role="review"),
    _row(3, section="s44"),
]


def _build_ledger(root: Path) -> Path:
    """Write the run documents and the aggregate that indexes them."""
    state = root / "docs" / "state" / PROJECT
    runs = state / "runs"
    runs.mkdir(parents=True)
    for row in ROWS:
        (runs / f"{row['run_id']}.json").write_text(json.dumps(row))
    (state / "crew.json").write_text(
        json.dumps({"data": {"runs": ROWS, "members": [], "holds": [], "_version": 1}})
    )
    return root


def test_the_projection_agrees_with_the_full_rows(tmp_path: Path) -> None:
    """The aggregate returns what the grouping returns over full history."""
    root = _build_ledger(tmp_path / "repo")
    full, _version = ledger.load(PROJECT, root)
    expected = mcp_views._group_section_attempts(PROJECT, full["runs"], [])
    actual = mcp_views.section_attempts_by_plan(PROJECT, root, pointers=[])
    assert actual == expected
    # The fixture proves the comparison bites: the failed row keeps its
    # classification, and the review row settles an id without becoming an
    # attempt.
    failed = [
        outcome
        for outcome in actual[PLAN]["s45"]["attempt_outcomes"]
        if outcome["status"] == "failed"
    ]
    assert failed and failed[0]["failure_classification"] == "test_failure"
    settled_non_executable = {row["run_id"] for row in ROWS if row["role"] == "review"}
    attempted = {
        outcome["run_id"]
        for record in actual[PLAN].values()
        for outcome in record["attempt_outcomes"]
    }
    assert settled_non_executable.isdisjoint(attempted)
    assert actual[PLAN]["s45"]["attempts"] == 2


def test_a_call_reads_no_full_payload(tmp_path: Path, monkeypatch) -> None:
    """Every index decode a call makes is the narrow projection."""
    root = _build_ledger(tmp_path / "repo")
    # Build the index before wrapping, so the fixture's own setup is not
    # counted as part of the read under test.
    ledger.load(PROJECT, root)

    calls: list[bool] = []
    original = ledger._index_payload

    def wrapped(connection, meta, *, headers_only, picker_only):
        calls.append(picker_only)
        return original(
            connection, meta, headers_only=headers_only, picker_only=picker_only
        )

    monkeypatch.setattr(ledger, "_index_payload", wrapped)
    mcp_views.section_attempts_by_plan(PROJECT, root, pointers=[])
    assert calls, "the read decoded nothing at all"
    assert all(calls), f"a call decoded a full payload: picker_only values {calls}"


def _previous_version() -> int:
    return ledger._RUN_INDEX_VERSION - 1


def test_an_index_from_the_previous_version_is_rebuilt_with_section(
    tmp_path: Path,
) -> None:
    """A narrower index is rebuilt on first read rather than served.

    The stored projection is blanked as well as the recorded version rolled
    back, so the ``section`` field can only come back from a rebuild: an index
    that was merely re-read would keep the blank column.
    """
    root = _build_ledger(tmp_path / "repo")
    ledger.load(PROJECT, root)
    index = ledger._run_index_path(PROJECT, root)

    with sqlite3.connect(index) as connection:
        connection.execute(f"PRAGMA user_version={_previous_version()}")
        connection.execute("UPDATE aggregate_rows SET picker='{}'")
        connection.execute("UPDATE records SET picker='{}'")
        connection.commit()
        recorded = connection.execute("PRAGMA user_version").fetchone()[0]
    assert recorded == _previous_version()

    rows = ledger.picker_runs(PROJECT, root)
    assert [row.get("section") for row in rows] == [row["section"] for row in ROWS]

    with sqlite3.connect(index) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == ledger._RUN_INDEX_VERSION


def _index_state(path: Path) -> tuple[int, int, int]:
    info = path.stat()
    return info.st_mtime_ns, info.st_ino, info.st_size


def test_two_versions_alternate_without_rebuilding_each_other(
    tmp_path: Path, monkeypatch
) -> None:
    """A host running two versions keeps one index per version.

    A session server and a worker can run different revisions for days. When
    both name one index file, each read at a version the file does not carry
    drops the other's tables and rebuilds from every run document, so two
    alternating readers rebuild on every switch. Counting the rebuilds — a
    rebuild re-reads each changed run document through ``_read_run`` and
    replaces the file — shows the second round rebuilding nothing.
    """
    root = _build_ledger(tmp_path / "repo")
    current = ledger._RUN_INDEX_VERSION
    previous = current - 1

    reads: list[int] = []
    real_read_run = ledger._read_run

    def counting(source):
        reads.append(1)
        return real_read_run(source)

    monkeypatch.setattr(ledger, "_read_run", counting)

    # Round one builds each version's own index; each build is expected.
    ledger.load(PROJECT, root)
    current_index = ledger._run_index_path(PROJECT, root)
    monkeypatch.setattr(ledger, "_RUN_INDEX_VERSION", previous)
    ledger.load(PROJECT, root)
    previous_index = ledger._run_index_path(PROJECT, root)
    assert current_index != previous_index
    assert current_index.is_file() and previous_index.is_file()

    before = {path: _index_state(path) for path in (current_index, previous_index)}
    reads.clear()

    # Round two alternates twice; neither index may move or be re-read into —
    # except the run-directory refresh, which reads only added documents.
    for version in (current, previous, current, previous):
        monkeypatch.setattr(ledger, "_RUN_INDEX_VERSION", version)
        ledger.picker_runs(PROJECT, root)

    after = {path: _index_state(path) for path in (current_index, previous_index)}
    assert before == after, "an alternating read rebuilt an index it did not own"
    assert not reads, f"the second round re-read {len(reads)} run documents"


def test_the_index_name_carries_its_version() -> None:
    """The version is in the file name, so two versions cannot collide."""
    path = ledger._run_index_path(PROJECT, None)
    assert path.name.endswith(f"-v{ledger._RUN_INDEX_VERSION}.sqlite")


def test_the_projection_carries_the_fields_the_grouping_reads() -> None:
    """The six fields the grouping reads are all named by the projection."""
    assert {"role", "run_id", "plan", "section", "gate", "failure_classification"} <= (
        set(ledger._PICKER_FIELDS)
    )


def _measured_figures() -> tuple[str, dict[str, float]]:
    """The fragment's verdict and the numbers it records as its measure."""
    text = FRAGMENT.read_text(encoding="utf-8")
    verdict = re.search(r'name="evidence-verdict" content="([^"]+)"', text)
    assert verdict is not None, "the fragment records no verdict"
    block = re.search(r'<p class="r-measure"([^>]*)>', text)
    assert block is not None, "the fragment records no measured figures"
    figures = {
        name: float(value)
        for name, value in re.findall(r'data-([a-z-]+)="([0-9.]+)"', block.group(1))
    }
    return verdict.group(1), figures


def test_the_fragment_verdict_agrees_with_its_measured_figures() -> None:
    """The recorded verdict may not claim more than the recorded measure.

    The section passes the node when the head's per-call time is at most one
    fifth of base and its resident memory is no more than 50 MB above base's.
    The fragment carries both figures and a verdict, so the two are checked
    against each other: a verdict of ``pass`` is refused while either bound is
    and a verdict naming a limitation is refused when every bound is met.
    """
    verdict, figures = _measured_figures()
    time_ratio = figures["head-seconds"] / figures["base-seconds"]
    profile_ratio = figures["head-profile-seconds"] / figures["base-profile-seconds"]
    rss_delta = figures["head-vmrss-kb"] - figures["base-vmrss-kb"]
    time_bound = figures["time-bound-ratio"]
    rss_bound = figures["rss-bound-kb"]

    meets_time = time_ratio <= time_bound and profile_ratio <= time_bound
    meets_memory = rss_delta <= rss_bound
    assert verdict in {"pass", "pass-with-limitation", "fail"}, verdict

    if verdict == "pass":
        assert meets_time, (
            f"verdict pass but per-call time is {time_ratio:.3f} of base, "
            f"{profile_ratio:.3f} in the profile, against a {time_bound} bound"
        )
        assert meets_memory, f"verdict pass but VmRSS is {rss_delta:,.0f} kB above base"
    elif verdict == "pass-with-limitation":
        assert not (meets_time and meets_memory), (
            "the verdict names a limitation but every bound is met"
        )
    else:
        assert not meets_time or not meets_memory, "verdict fail but every bound is met"
