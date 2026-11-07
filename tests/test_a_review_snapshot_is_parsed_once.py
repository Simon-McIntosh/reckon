"""A stored review snapshot's evidence is parsed once, across processes.

``plan_review._snapshot_evidence`` recomposes a stored review's unit digests,
prose texts and section declarations from the snapshot the reviewer read. Each
call parsed the same snapshot four times, through two parsers, and nothing was
kept between calls or between processes, so a restart paid for every snapshot
again.

The evidence is now persisted under the snapshot's content digest, guarded by
the source digests of the modules that produced it. The second simulated
process below clears the in-process memo and asserts that neither parser is
reached for a snapshot the first process already stored, while both processes
return the evidence the base revision's composition returns.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _plan_html, file_memo, metadata_index
from reckon.crew import plan_review

PROJECT = "sample"
SNAPSHOT_COUNT = 10


def _snapshot_text(number: int) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="plan-{number:02d}">'
        f'<meta name="plan-title" content="snapshot {number}">'
        '<meta name="plan-summary" content="a reviewed snapshot">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-impl" content="0.5">'
        '<meta name="plan-version" content="1">'
        '<meta name="plan-section-declarations" '
        'content="{&quot;s1&quot;: &quot;implementable&quot;, '
        '&quot;s2&quot;: &quot;implemented&quot;}">'
        '</head><body><main class="plan-doc">'
        f'<h2 id="s1">§1 — the first section of {number}</h2>'
        f"<p>authored prose for unit one, snapshot {number}</p>"
        f'<h2 id="s2">§2 — the second section of {number}</h2>'
        f"<p>authored prose for unit two, snapshot {number}</p>"
        "</main></body></html>"
    )


class SnapshotStore:
    """Ten stored reviews, each with the plan.html snapshot it read."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.home.mkdir(parents=True)
        self.records: list[dict[str, str]] = []
        self.texts: list[str] = []
        for number in range(SNAPSHOT_COUNT):
            slug = f"plan-{number:02d}"
            run_id = f"r-20260101T0000{number:02d}Z-review-of-{slug}"
            directory = plan_review.review_report_directory(PROJECT, slug, run_id)
            directory.mkdir(parents=True, exist_ok=True)
            text = _snapshot_text(number)
            (directory / plan_review._REVIEW_SNAPSHOT_NAME).write_text(
                text, encoding="utf-8"
            )
            self.texts.append(text)
            self.records.append(
                {
                    "review_run_id": run_id,
                    "plan_version": "1",
                    "slug": slug,
                }
            )

    def slug(self, index: int) -> str:
        return self.records[index]["slug"]


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SnapshotStore:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    state = SnapshotStore(tmp_path)
    _forget_process()
    return state


class ParseCounts:
    """Each parse either parser performs, recorded through its module."""

    def __init__(self) -> None:
        self.read_state: list[str] = []
        self.structural_spans: list[str] = []

    def clear(self) -> None:
        self.read_state.clear()
        self.structural_spans.clear()

    @property
    def total(self) -> int:
        return len(self.read_state) + len(self.structural_spans)


@pytest.fixture
def counts(monkeypatch: pytest.MonkeyPatch) -> ParseCounts:
    recorded = ParseCounts()
    real_read_state = _plan_html.read_state
    real_structural_spans = _plan_html._structural_spans

    def counting_read_state(text: str) -> dict:
        recorded.read_state.append(text)
        return real_read_state(text)

    def counting_structural_spans(text: str):
        recorded.structural_spans.append(text)
        return real_structural_spans(text)

    monkeypatch.setattr(_plan_html, "read_state", counting_read_state)
    monkeypatch.setattr(_plan_html, "_structural_spans", counting_structural_spans)
    return recorded


def _forget_process() -> None:
    """Drop every in-process memo, as a restarted process starts empty."""

    file_memo.clear()
    metadata_index._CACHE.clear()


def _base_evidence(text: str):
    """The evidence the base revision composed, in one call each.

    The base revision's ``_snapshot_evidence`` returned exactly this composition
    for a snapshot's text, so comparing against it compares against the base
    without depending on the memo under test.
    """

    return (
        plan_review._section_digests(text),
        plan_review._prose_texts(text),
        plan_review._snapshot_declarations(text),
    )


def _evidence_all(store: SnapshotStore) -> list:
    return [
        plan_review._snapshot_evidence(PROJECT, store.slug(index), record)
        for index, record in enumerate(store.records)
    ]


def test_a_second_process_parses_no_stored_snapshot(
    store: SnapshotStore, counts: ParseCounts
):
    """A restarted process answers every snapshot from the persisted evidence."""

    expected = [_base_evidence(text) for text in store.texts]
    assert all(item is not None for item in expected)
    assert all(item[2] for item in expected), (
        "positive control: every snapshot carries its own declarations"
    )
    counts.clear()

    first = _evidence_all(store)

    assert len(counts.read_state) == 2 * SNAPSHOT_COUNT, (
        "expected the digest state and the declarations to parse each snapshot, "
        f"saw {len(counts.read_state)} read_state calls"
    )
    assert len(counts.structural_spans) >= SNAPSHOT_COUNT, (
        "positive control: the prose parser must have been reached"
    )

    counts.clear()
    _forget_process()

    second = _evidence_all(store)

    assert counts.read_state == [], (
        f"{len(counts.read_state)} snapshots were re-parsed by read_state"
    )
    assert counts.structural_spans == [], (
        f"{len(counts.structural_spans)} snapshots were re-parsed by the span reader"
    )
    for index, (before, after, base) in enumerate(
        zip(first, second, expected, strict=True)
    ):
        assert before is not None and after is not None, (
            f"snapshot {index} lost evidence"
        )
        assert before[0] == base[0], f"snapshot {index} digests differ from the base"
        assert before[1] == base[1], f"snapshot {index} prose differs from the base"
        assert before[2] == base[2], (
            f"snapshot {index} declarations differ from the base"
        )
        assert after[0] == base[0], f"snapshot {index} digests differ after a restart"
        assert after[1] == base[1], f"snapshot {index} prose differs after a restart"
        assert after[2] == base[2], (
            f"snapshot {index} declarations differ after a restart"
        )
        assert json.dumps(after[2], sort_keys=True) == json.dumps(
            base[2], sort_keys=True
        )


def test_a_parser_change_invalidates_the_stored_evidence(
    store: SnapshotStore, counts: ParseCounts, monkeypatch: pytest.MonkeyPatch
):
    """Evidence recorded by another revision of the parser is treated as absent."""

    first = _evidence_all(store)
    assert all(item is not None for item in first)
    assert counts.total > 0, "positive control: the first pass must have parsed"

    counts.clear()
    _forget_process()

    real = metadata_index._module_source_digest
    changed = real(_plan_html) + "-moved"

    def moved(module: object) -> str:
        return changed if module is _plan_html else real(module)

    monkeypatch.setattr(metadata_index, "_module_source_digest", moved)

    second = _evidence_all(store)

    assert len(counts.read_state) == 2 * SNAPSHOT_COUNT, (
        f"expected every snapshot re-parsed, saw {len(counts.read_state)}"
    )
    for index, (before, after) in enumerate(zip(first, second, strict=True)):
        assert before == after, f"snapshot {index} changed under a parser move"
