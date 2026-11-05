"""Closing names its remedies, and a move carries the section's state.

``close`` still refuses a plan holding a deferred section, but the refusal now
names both ways to clear one: a descoped declaration carrying a reason, or a
descoped declaration carrying ``moved_to`` naming the successor section. The
second is a move, not a drop, so the successor receives the section's prose,
effort, capability and open gates, its comments are re-anchored on the move
record this write leaves, and the successor lists the move as an incoming link.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

import reckon._store as _store_module
import reckon.mcp as mcp_module
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "close-move-project"
SLUG = "close-move-case"
SUCCESSOR = "successor-case"

_CAPABILITY = (
    ' data-capability-version="1.0" data-capability-class="general"'
    ' data-capability-reasoning="{reasoning}"'
    ' data-capability-verification="{verification}"'
    ' data-capability-risk="{risk}"'
)


@pytest.fixture()
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps({PROJECT: str(docs_dir)}))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))
    serve_mod = pytest.importorskip("reckon.serve")
    serve_mod._MOUNTS_FILE = mounts_file
    serve_mod._STATE_ROOT = state_root
    reload_mcp_family()
    return docs_dir, PROJECT


def _record(section: str, effort: float, status: str, capability: dict) -> str:
    return (
        f'<section data-reckon="section" data-id="{section}"'
        f' data-effort-hours="{effort}"'
        + _CAPABILITY.format(**capability)
        + f' data-status="{status}" data-links=""></section>'
    )


def _plan_html(
    slug: str,
    sections: list[tuple[str, float, str, dict, str]],
    *,
    gates: str = "",
    comments: str = "",
    declarations: dict[str, str] | None = None,
) -> str:
    body = []
    for section, effort, status, capability, prose in sections:
        body.append(f'<h2 id="{section}">§ {section}</h2>')
        body.append(_record(section, effort, status, capability))
        body.append(f"<p>{prose}</p>")
    declarations = declarations or {s[0]: s[2] for s in sections}
    return (
        '<!doctype html>\n<html lang="en">\n<head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        f'<meta name="plan-slug" content="{slug}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-status" content="active">'
        f'<meta name="plan-version" content="0">'
        '<meta name="plan-section-declarations" content=\''
        + json.dumps(declarations)
        + "'>"
        f'<title>{slug}</title></head>\n<body><main class="plan-doc">'
        + "\n".join(body)
        + (
            '<section data-reckon="gates" id="gates"><h2>Gates</h2>'
            + gates
            + "</section>"
            if gates
            else ""
        )
        + '<section data-reckon="comments" id="comments"><h2>Comments</h2>'
        + comments
        + "</section>"
        + "</main></body></html>\n"
    )


def _write(docs_dir: Path, slug: str, text: str) -> Path:
    path = docs_dir / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _write_evidence(docs_dir: Path, slug: str) -> None:
    path = docs_dir / "evidence" / "archive" / f"{slug}-landed.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        f'<meta name="plan-slug" content="{slug}-landed">'
        '<meta name="reckon-type" content="evidence">'
        f'<meta name="plan-evidence-for" content="{slug}">'
        "</head><body><main></main></body></html>\n",
        encoding="utf-8",
    )


_SOURCE_SECTIONS = [
    (
        "s1",
        1.0,
        "deferred",
        {
            "reasoning": "standard",
            "verification": "standard",
            "risk": "low",
        },
        "The first dropped idea.",
    ),
    (
        "s2",
        1.25,
        "deferred",
        {
            "reasoning": "deep",
            "verification": "strict",
            "risk": "moderate",
        },
        "The prose that must follow the work.",
    ),
]

_OPEN_GATE = (
    '<div class="r-gate" data-id="g-moved" data-section="s2"'
    ' data-gated-sections="s2" data-status="open" data-verdict="">'
    '<h4 class="r-gate-measure">the moved gate</h4>'
    '<p class="r-gate-required-evidence">evidence</p></div>'
)

_CLOSED_GATE = (
    '<div class="r-gate" data-id="g-closed" data-section="s2"'
    ' data-gated-sections="s2" data-status="closed" data-verdict="passed">'
    '<h4 class="r-gate-measure">the settled gate</h4>'
    '<p class="r-gate-required-evidence">evidence</p></div>'
)

_ANCHORED_COMMENT = (
    '<div class="r-comment" data-section="s2" data-id="c1" data-who="agent"'
    ' data-when="2026-11-01"><div class="r-comment-body">note on s2</div></div>'
)


def _source_plan() -> str:
    return _plan_html(
        SLUG,
        _SOURCE_SECTIONS,
        gates=_OPEN_GATE,
        comments=_ANCHORED_COMMENT,
        declarations={"s1": "deferred", "s2": "deferred"},
    )


def _successor_plan() -> str:
    return _plan_html(
        SUCCESSOR,
        [
            (
                "s9",
                0.5,
                "implementable",
                {
                    "reasoning": "standard",
                    "verification": "standard",
                    "risk": "low",
                },
                "The successor's own prose.",
            )
        ],
    )


def _setup_trees(docs_dir: Path) -> None:
    _write(docs_dir, SLUG, _source_plan())
    _write(docs_dir, SUCCESSOR, _successor_plan())
    _write_evidence(docs_dir, SLUG)


def _close(project: str) -> dict:
    return mcp_module._edit_plan(
        project,
        SLUG,
        [
            {
                "op": "close",
                "status": "done",
                "outcome": "the remaining work moved — no followup",
                "by": "reckon-build",
            }
        ],
        _store_module.read_plan(project, SLUG)[1],
    )


def test_the_refusal_names_both_sections_and_both_remedies(setup) -> None:
    docs_dir, project = setup
    _setup_trees(docs_dir)

    result = _close(project)

    assert result["ok"] is False, result
    detail = result["detail"]
    assert "s1" in detail and "s2" in detail, "both deferred sections are named"
    assert "descoped" in detail, "the descope declaration is named"
    assert "reason" in detail, "the reason remedy is named"
    assert "moved_to" in detail, "the move: remedy is named"


def test_a_move_carries_the_sections_state(setup) -> None:
    docs_dir, project = setup
    _setup_trees(docs_dir)

    first = mcp_module._edit_plan(
        project,
        SLUG,
        [
            {
                "op": "set",
                "path": "section_declarations.s1",
                "value": {"status": "descoped", "reason": "no longer needed"},
            }
        ],
        0,
    )
    assert first["ok"] is True, first

    second = mcp_module._edit_plan(
        project,
        SLUG,
        [
            {
                "op": "set",
                "path": "section_declarations.s2",
                "value": {
                    "status": "descoped",
                    "reason": "the successor owns this now",
                    "moved_to": f"{SUCCESSOR}#s9",
                },
            }
        ],
        _store_module.read_plan(project, SLUG)[1],
    )
    assert second["ok"] is True, second

    closed = _close(project)
    assert closed["ok"] is True, closed

    source, _ = _store_module.read_plan(project, SLUG)
    target, _ = _store_module.read_plan(project, SUCCESSOR)

    successor = _store_module._section_record(target, "s9")
    assert successor is not None
    assert f"{SLUG}#s2" in successor["links"], "the move is an incoming link"
    assert successor["capability"]["requirements"]["reasoning"] == "deep"
    assert successor["capability"]["requirements"]["verification"] == "strict"
    assert successor["capability"]["requirements"]["risk"] == "moderate"

    target_text = (docs_dir / "plans" / f"{SUCCESSOR}.html").read_text()
    assert "The prose that must follow the work." in target_text
    assert "The successor's own prose." in target_text
    record_end = target_text.index("</section>", target_text.index('<h2 id="s9"'))
    assert target_text[record_end:].startswith(
        "</section>\n<p>§ s2 The prose that must follow the work.</p>"
    ), "the carried paragraph lands after the record, not between it and the heading"

    carried = [g for g in target.get("gates") or [] if g["id"] == "g-moved"]
    assert carried, "the open gate moved with the section"
    assert carried[0]["gated_sections"] == ["s9"]

    assert successor["effort_hours"] == pytest.approx(1.75), (
        "the moved section's effort is added to the successor's own"
    )

    source_sections = set(source.get("section_declarations") or {})
    assert all(
        set(gate.get("gated_sections") or []) <= source_sections
        for gate in source.get("gates") or []
    ), "no gate on the closed plan names a section it does not declare"

    assert "s2" not in (source.get("comments") or {}), (
        "the moved section's comments are re-anchored"
    )
    moved = (source.get("comments") or {}).get("c-move-s2") or []
    assert any(record.get("id") == "c1" for record in moved), (
        "the anchored comment travels to the move record"
    )


def test_only_open_gates_travel_and_a_closed_gate_stays(setup) -> None:
    docs_dir, project = setup
    _write(
        docs_dir,
        SLUG,
        _plan_html(
            SLUG,
            _SOURCE_SECTIONS,
            gates=_OPEN_GATE + _CLOSED_GATE,
            comments=_ANCHORED_COMMENT,
            declarations={"s1": "deferred", "s2": "deferred"},
        ),
    )
    _write(docs_dir, SUCCESSOR, _successor_plan())
    _write_evidence(docs_dir, SLUG)

    result = mcp_module._edit_plan(
        project,
        SLUG,
        [
            {
                "op": "set",
                "path": "section_declarations.s2",
                "value": {
                    "status": "descoped",
                    "reason": "the successor owns this now",
                    "moved_to": f"{SUCCESSOR}#s9",
                },
            }
        ],
        0,
    )
    assert result["ok"] is True, result

    source, _ = _store_module.read_plan(project, SLUG)
    target, _ = _store_module.read_plan(project, SUCCESSOR)

    carried_ids = [g["id"] for g in target.get("gates") or []]
    assert "g-moved" in carried_ids, "the open gate travels with the work"
    assert "g-closed" not in carried_ids, (
        "a settled gate is an outcome, not work to carry"
    )

    source_ids = [g["id"] for g in source.get("gates") or []]
    assert "g-closed" in source_ids, (
        "the closed gate stays on the closed plan as evidence"
    )


def test_a_move_resolves_the_successor_against_the_checkout_being_written(
    setup, tmp_path: Path
) -> None:
    docs_dir, project = setup
    _setup_trees(docs_dir)
    # The mounts-resolved successor carries no s9, so a move that resolved
    # against the default root instead of the checkout being written is refused
    # here instead of silently carrying the work into the wrong tree.
    _write(
        docs_dir,
        SUCCESSOR,
        _plan_html(
            SUCCESSOR,
            [
                (
                    "other",
                    0.5,
                    "implementable",
                    {
                        "reasoning": "standard",
                        "verification": "standard",
                        "risk": "low",
                    },
                    "A decoy section.",
                )
            ],
        ),
    )
    checkout = tmp_path / "checkout"
    checkout_docs = checkout / "docs"
    _write(checkout_docs, SLUG, _source_plan())
    _write(checkout_docs, SUCCESSOR, _successor_plan())

    result = mcp_module._edit_plan(
        project,
        SLUG,
        [
            {
                "op": "set",
                "path": "section_declarations.s2",
                "value": {
                    "status": "descoped",
                    "reason": "the successor owns this now",
                    "moved_to": f"{SUCCESSOR}#s9",
                },
            }
        ],
        0,
    )
    assert result["ok"] is False, "the mounts tree declares no such successor section"

    result = mcp_module._edit_plan(
        project,
        SLUG,
        [
            {
                "op": "set",
                "path": "section_declarations.s2",
                "value": {
                    "status": "descoped",
                    "reason": "the successor owns this now",
                    "moved_to": f"{SUCCESSOR}#s9",
                },
            }
        ],
        0,
        checkout_path=str(checkout),
    )
    assert result["ok"] is True, result

    written = (checkout_docs / "plans" / f"{SUCCESSOR}.html").read_text()
    assert "The prose that must follow the work." in written, (
        "the move landed in the checkout that was written"
    )
    assert (
        "A decoy section." in (docs_dir / "plans" / f"{SUCCESSOR}.html").read_text()
    ), "the default tree is untouched"


def test_a_refused_move_writes_nothing_not_even_its_evidence(setup) -> None:
    docs_dir, project = setup
    _setup_trees(docs_dir)
    record = docs_dir / "evidence" / "archive" / f"{SLUG}-landed.html"
    before = record.read_text()
    version_before = _store_module.read_plan(project, SLUG)[1]

    result = mcp_module._edit_plan(
        project,
        SLUG,
        [
            {
                "op": "append_evidence",
                "plan": SLUG,
                "anchor": "a-move",
                "title": "the move",
                "body": "<p>a paragraph that must not land</p>",
            },
            {
                "op": "set",
                "path": "section_declarations.s2",
                "value": {
                    "status": "descoped",
                    "reason": "the successor owns this now",
                    "moved_to": f"{SLUG}#s1",
                },
            },
        ],
        0,
    )

    assert result["ok"] is False, "a successor in the same plan is refused"
    assert "another plan" in result["detail"]
    assert record.read_text() == before, "the evidence append wrote nothing"
    assert "a-move" not in record.read_text()
    assert _store_module.read_plan(project, SLUG)[1] == version_before


def test_a_moved_to_that_does_not_resolve_is_refused(setup) -> None:
    docs_dir, project = setup
    _setup_trees(docs_dir)

    result = mcp_module._edit_plan(
        project,
        SLUG,
        [
            {
                "op": "set",
                "path": "section_declarations.s1",
                "value": {
                    "status": "descoped",
                    "reason": "some of it moves",
                    "moved_to": f"{SUCCESSOR}#nope",
                },
            }
        ],
        0,
    )

    assert result["ok"] is False, result
    assert "moved_to" in result["detail"]
    assert "nope" in result["detail"]


def test_a_payload_without_a_slug_cannot_defeat_the_same_plan_refusal(setup) -> None:
    docs_dir, project = setup
    _setup_trees(docs_dir)
    path = docs_dir / "plans" / f"{SLUG}.html"
    before = path.read_text()
    version_before = _store_module.read_plan(project, SLUG)[1]

    # The store is written to directly here, below the editor's schema check,
    # because that is where the refusal lives and the payload's identity must
    # not be able to stand in for the caller's.
    state = dict(_store_module.read_plan(project, SLUG)[0])
    state.pop("slug", None)
    ops = [
        {
            "op": "set",
            "path": "section_declarations.s2",
            "value": {
                "status": "descoped",
                "reason": "the successor owns this now",
                "moved_to": f"{SLUG}#s1",
            },
        }
    ]

    outcome: dict = {}

    def _attempt() -> None:
        # The batch and its write run in one thread: a batch's write effects are
        # held per context, so applying the ops elsewhere leaves the store with
        # no move to judge.
        try:
            _store_module.apply_ops(state, ops, False)
            outcome["version"] = _store_module.write_plan(
                project, SLUG, state, version_before
            )
        except _store_module.OpError as exc:
            outcome["error"] = exc

    # Bounded: a guard that reads the payload's identity lets this move through
    # and the successor's half re-enters this plan's own write, which either
    # lands as a second write or blocks on the lock this one holds. A regression
    # must fail here rather than hang the suite.
    attempt = threading.Thread(target=_attempt, daemon=True)
    attempt.start()
    attempt.join(timeout=30)

    assert not attempt.is_alive(), "the write did not stop at the refusal"
    error = outcome.get("error")
    assert isinstance(error, _store_module.OpError), outcome
    assert "another plan" in str(error)
    assert path.read_text() == before, "the refused write left the document alone"
    assert _store_module.read_plan(project, SLUG)[1] == version_before


def test_carried_prose_lands_after_an_adjacent_record() -> None:
    document = (
        '<main><h2 id="s9">&#167; s9</h2>'
        '<section data-reckon="section" data-id="s9" data-status="implementable">'
        "</section><p>own prose</p></main>"
    )

    placed = _store_module._insert_section_prose(document, "s9", "carried prose")

    assert placed == (
        '<main><h2 id="s9">&#167; s9</h2>'
        '<section data-reckon="section" data-id="s9" data-status="implementable">'
        "</section>\n<p>carried prose</p><p>own prose</p></main>"
    ), "the paragraph goes after the record, never between it and its heading"
    assert "</section></h2>" not in placed


def test_carried_prose_lands_after_the_heading_without_a_record() -> None:
    document = '<main><h2 id="s9">&#167; s9</h2><p>own prose</p></main>'

    placed = _store_module._insert_section_prose(document, "s9", "carried prose")

    assert placed == (
        '<main><h2 id="s9">&#167; s9</h2>\n<p>carried prose</p><p>own prose</p></main>'
    ), "with no record the paragraph follows the heading itself"
