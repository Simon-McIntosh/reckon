"""read_plan answers a followup wait the way the roadmap does.

A section can wait on one followup of another plan (``slug@f-id``), and the
roadmap holds it while that followup is open, reporting a
``dangling-hard-dependency`` when the target plan carries no such followup. A
``read_plan`` of the waiting plan gives a different answer: its dependency rows
report the target plan's or section's state and never the followup the ref
names, so a coordinator asking why a section is held is told the target plan is
active, and is told nothing at all when the followup does not exist.

These cases read both surfaces over the same on-disk project and compare them
row for row, rather than asserting either surface's literals: the roadmap's
section-edge rows are the answer the read_plan rows must carry. They fail
where ``read_plan`` resolves a followup ref by the plan alone, which is what
the rows read before the followup half had one derivation.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

import pytest

from reckon import file_memo
from reckon._plan_html import write_state
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "probe"
SPRINT = "S1"
WAITING = "waiting"
TARGET = "other"

#: The target's section declarations say ``implementable`` while its plan
#: status is ``active``, so a row that reported the waited section's state for
#: a staged ref would disagree with the plan status the roadmap reports.
WAITED_SECTION = "s2"
TARGET_SECTION_STATE = "implementable"
TARGET_PLAN_STATUS = "active"

FOLLOWUP_STATES = (
    ("f-open", "open", True),
    ("f-resolved", "resolved", True),
    ("f-missing", "", False),
)
BARE_FOLLOWUP_REFS = tuple(
    f"{TARGET}@{followup_id}" for followup_id, _, _ in FOLLOWUP_STATES
)
STAGED_FOLLOWUP_REFS = tuple(
    f"{TARGET}#{WAITED_SECTION}@{followup_id}" for followup_id, _, _ in FOLLOWUP_STATES
)
FOLLOWUP_REFS = BARE_FOLLOWUP_REFS + STAGED_FOLLOWUP_REFS
PLAIN_OPEN_REF = f"{TARGET}#{WAITED_SECTION}"

BARE = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    f'<meta name="docs-project" content="{PROJECT}">'
    "<title>{slug}</title>{meta}</head>"
    '<body><main class="plan-doc"></main></body></html>'
)


def _document(
    slug: str,
    *,
    section_depends_on: dict | None = None,
    followups: list[tuple[str, str]] | None = None,
    sections: dict[str, str] | None = None,
) -> str:
    state = {
        "slug": slug,
        "title": slug,
        "status": "active",
        "type": "plan",
        "version": 0,
        "sprint": SPRINT,
        "section_declarations": sections or {"s1": "implementable"},
        "followups": [
            {"id": followup_id, "status": status, "prompt": "/reckon-edit y"}
            for followup_id, status in followups or []
        ],
    }
    meta = ""
    if section_depends_on is not None:
        meta = (
            '<meta name="plan-section-depends-on" content="'
            + html.escape(json.dumps(section_depends_on), quote=True)
            + '">'
        )
    return write_state(BARE.format(slug=slug, meta=meta), state)


def _build_checkout(root: Path) -> Path:
    docs = root / "docs"
    plans = docs / "plans"
    state = docs / "state" / PROJECT
    plans.mkdir(parents=True)
    state.mkdir(parents=True)
    (plans / f"{WAITING}.html").write_text(
        _document(
            WAITING,
            section_depends_on={"s1": [*FOLLOWUP_REFS, PLAIN_OPEN_REF]},
        ),
        encoding="utf-8",
    )
    (plans / f"{TARGET}.html").write_text(
        _document(
            TARGET,
            followups=[("f-open", "open"), ("f-resolved", "resolved")],
            sections={"s2": "implementable"},
        ),
        encoding="utf-8",
    )
    (state / "index.json").write_text(
        json.dumps(
            {
                "project": PROJECT,
                "doc": "index",
                "data": {
                    "_version": 0,
                    "sprints": [
                        {
                            "id": SPRINT,
                            "status": "active",
                            "items": [WAITING, TARGET],
                        }
                    ],
                    "projects": [{"project": PROJECT}],
                },
            }
        ),
        encoding="utf-8",
    )
    return root


@pytest.fixture()
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("RECKON_RUN_ID", raising=False)
    monkeypatch.delenv("RECKON_MANIFEST", raising=False)
    file_memo.clear()
    reload_mcp_family()
    yield _build_checkout(tmp_path / "checkout")
    file_memo.clear()
    reload_mcp_family()


def _read_plan_rows(root: Path) -> list[dict]:
    from reckon import mcp

    payload = mcp._read_plan(PROJECT, WAITING, checkout_path=str(root))
    assert payload.get("ok", True) is not False, payload
    return [row for row in payload.get("deps") or [] if row.get("source_section")]


def _roadmap_rows(root: Path) -> list[dict]:
    from reckon import mcp

    report = mcp._roadmap(PROJECT, checkout_path=str(root), view="raw")
    data = report.get("data") or report
    for bucket in ("ready_now", "blocked", "deferred", "pending_work"):
        for row in data.get(bucket) or []:
            if row.get("slug") == WAITING:
                return [
                    edge
                    for edge in row.get("depends_on") or []
                    if edge.get("source_section")
                ]
    raise AssertionError(f"{WAITING} is in no roadmap bucket")


def _by_ref(rows: list[dict], ref: str) -> dict:
    for row in rows:
        if row.get("ref") == ref:
            return row
    raise AssertionError(f"no row for ref {ref!r} among {[r.get('ref') for r in rows]}")


WAIT_KEYS = ("followup", "followup_found", "followup_status", "satisfied")

#: Every key the two surfaces must report alike for a followup ref, in either
#: form. ``status`` is included because a staged ref (``slug#sN@f-id``) is the
#: case where the two surfaces used to disagree: the section half names where
#: the followup belongs, not what is being waited on.
COMPARED_KEYS = ("scope", "slug", "found", "status", *WAIT_KEYS)


def _compared(row: dict) -> dict:
    return {key: row.get(key) for key in COMPARED_KEYS}


@pytest.mark.parametrize("ref", FOLLOWUP_REFS)
def test_read_plan_reports_the_followup_wait_the_roadmap_reports(
    checkout: Path, ref: str
) -> None:
    read_row = _by_ref(_read_plan_rows(checkout), ref)
    roadmap_row = _by_ref(_roadmap_rows(checkout), ref)

    assert _compared(read_row) == _compared(roadmap_row)
    assert read_row.get("followup") == ref.split("@", 1)[1]


@pytest.mark.parametrize("ref", FOLLOWUP_REFS)
def test_the_target_plan_status_is_what_a_followup_row_reports(
    checkout: Path, ref: str
) -> None:
    """A staged ref reports the plan's status, not the waited section's state.

    The target's section declares ``implementable`` while its plan is
    ``active``, so a row built from the section half alone is distinguishable
    from one built from the followup wait.
    """

    read_row = _by_ref(_read_plan_rows(checkout), ref)

    assert read_row["status"] == TARGET_PLAN_STATUS
    assert read_row["status"] != TARGET_SECTION_STATE


@pytest.mark.parametrize("ref", FOLLOWUP_REFS)
def test_the_followup_state_decides_the_wait(checkout: Path, ref: str) -> None:
    read_row = _by_ref(_read_plan_rows(checkout), ref)
    roadmap_row = _by_ref(_roadmap_rows(checkout), ref)
    followup_id = ref.split("@", 1)[1]
    found, followup_status, satisfied = {
        state_id: (is_found, state, is_found and state != "open")
        for state_id, state, is_found in FOLLOWUP_STATES
    }[followup_id]

    assert read_row["followup"] == followup_id
    assert read_row["followup_found"] is found
    assert read_row["followup_status"] == followup_status
    assert read_row["satisfied"] is satisfied
    assert read_row["satisfied"] == roadmap_row["satisfied"]


@pytest.mark.parametrize(
    "followup_id", [followup_id for followup_id, _, _ in FOLLOWUP_STATES]
)
def test_a_staged_ref_reads_as_its_bare_form(checkout: Path, followup_id: str) -> None:
    """The section half adds provenance, never a different answer.

    Only the ``ref`` string differs between the two rows; the wait's four
    answers and the reported status are the same, so a caller that stages a
    ref does not silently change what the row says.
    """

    bare_row = _by_ref(_read_plan_rows(checkout), f"{TARGET}@{followup_id}")
    staged_row = _by_ref(
        _read_plan_rows(checkout), f"{TARGET}#{WAITED_SECTION}@{followup_id}"
    )

    assert {key: staged_row[key] for key in COMPARED_KEYS} == {
        key: bare_row[key] for key in COMPARED_KEYS
    }


@pytest.mark.parametrize("ref", [ref for ref in FOLLOWUP_REFS if "@f-missing" in ref])
def test_a_missing_followup_is_reported_as_one_by_both_surfaces(
    checkout: Path, ref: str
) -> None:
    from reckon import mcp

    read_row = _by_ref(_read_plan_rows(checkout), ref)
    roadmap_row = _by_ref(_roadmap_rows(checkout), ref)

    assert read_row["followup_found"] is False
    assert read_row["followup_status"] == roadmap_row["followup_status"]
    assert read_row["satisfied"] is False
    assert read_row["satisfied"] == roadmap_row["satisfied"]

    report = mcp._roadmap(PROJECT, checkout_path=str(checkout), view="raw")
    data = report.get("data") or report
    dangling = [
        finding
        for finding in data.get("wiring_findings") or []
        if finding.get("code") == "dangling-hard-dependency"
        and (finding.get("extra") or {}).get("ref") == ref
    ]
    assert dangling, [
        finding.get("code") for finding in data.get("wiring_findings") or []
    ]


def test_a_ref_without_a_followup_half_reads_the_same_row(checkout: Path) -> None:
    read_row = _by_ref(_read_plan_rows(checkout), PLAIN_OPEN_REF)
    roadmap_row = _by_ref(_roadmap_rows(checkout), PLAIN_OPEN_REF)

    assert not [key for key in WAIT_KEYS if key in read_row]
    for key in ("scope", "slug", "found", "status"):
        assert read_row.get(key) == roadmap_row.get(key)
    assert read_row.get("stage") == "s2"


def test_the_waiting_row_carries_its_section_beside_its_dependencies(
    checkout: Path,
) -> None:
    read_rows = _read_plan_rows(checkout)
    roadmap_rows = _roadmap_rows(checkout)

    assert {row.get("ref") for row in read_rows} == {
        row.get("ref") for row in roadmap_rows
    }
