"""A re-synthesis carries every appended anchor and names every loss.

An evidence append writes an anchored section into a plan's landed record, and
a link into the plan points at that anchor. A synthesis rewrites the record from
the plan's comments and the ledger, so an anchor it does not render is a live
link it silently kills. The checks below hold a record written by an earlier
synthesis, show which sections a rewrite carries, and show that an append onto
an id the record renders — or may render — is refused before it is written.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from click.testing import CliRunner

from reckon import _plan_html, evidence, ledger
from reckon import mcp as mcp_module
from reckon.cli import main

PROJECT = "sample"
PLAN = "demo-plan"
TITLE = "Demo plan"

CAPABILITY = {
    "version": "1.0",
    "class": "general",
    "requirements": {
        "reasoning": "standard",
        "verification": "strict",
        "risk": "low",
    },
}

#: Heading ids the authored plan carries. ``s3`` is declared and holds nothing,
#: so no synthesis renders it; ``s5-1`` is a numbered section an append may
#: also spell ``s5.1``.
_HEADINGS = ("delivery", "verification", "s3", "s4", "s5-1")


def _authored() -> str:
    headings = "".join(
        f'<h2 id="{section_id}">{section_id} outcome</h2><p>{section_id} prose.</p>'
        for section_id in _HEADINGS
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f"<title>{TITLE}</title></head><body>"
        f'<main class="plan-doc">'
        f"{headings}"
        "</main></body></html>"
    )


def _state(comments: dict | None = None) -> dict:
    sections = [
        {
            "id": section_id,
            "effort_hours": 1.0,
            "capability": deepcopy(CAPABILITY),
            "attempts": 0,
            "status": (
                "done"
                if section_id in ("delivery", "verification")
                else "implementable"
            ),
            "links": [],
        }
        for section_id in _HEADINGS
    ]
    return {
        "project": PROJECT,
        "type": "plan",
        "slug": PLAN,
        "title": TITLE,
        "status": "active",
        "modified": "2026-11-07",
        "version": 0,
        "section_declarations": {
            section_id: (
                "done"
                if section_id in ("delivery", "verification")
                else "implementable"
            )
            for section_id in _HEADINGS
        },
        "sections": sections,
        "followups": [],
        "comments": dict(comments or {}),
    }


def _plan_file(checkout: Path) -> Path:
    return checkout / "docs" / "plans" / f"{PLAN}.html"


def _write_plan(checkout: Path, comments: dict | None = None) -> Path:
    path = _plan_file(checkout)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _plan_html.write_state(_authored(), _state(comments)), encoding="utf-8"
    )
    return path


def _run_record(
    run_id: str, section: str, completed_at: str, commits: tuple[str, ...]
) -> dict:
    return ledger.build_record(
        run_id=run_id,
        plan=PLAN,
        section=section,
        node=f"{section}-node",
        gate="passed",
        completed_at=completed_at,
        completed_at_source="provided",
        worker_seconds=60,
        commits=commits,
        tests_added=1,
        changed_lines={"insertions": 4, "deletions": 1},
    )


@pytest.fixture()
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    root = tmp_path / "repo"
    _write_plan(root)
    ledger.write(PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=root)
    ledger.append_run(
        PROJECT,
        _run_record("run-delivery", "delivery", "2026-11-07T10:00:00Z", ("aaaaaaa",)),
        root=root,
    )
    ledger.append_run(
        PROJECT,
        _run_record(
            "run-verification", "verification", "2026-11-07T11:00:00Z", ("bbbbbbb",)
        ),
        root=root,
    )
    return root


def _record_path(checkout: Path) -> Path:
    return checkout / "docs" / "evidence" / "archive" / f"{PLAN}-landed.html"


def _synthesize(checkout: Path):
    return evidence.synthesize_landed_record(checkout / "docs", PROJECT, PLAN)


def _append(checkout: Path, anchor: str, body: str) -> dict:
    path = _plan_file(checkout)
    state = _plan_html.read_state(path.read_text(encoding="utf-8"))
    return mcp_module._edit_plan_tool(
        PROJECT,
        PLAN,
        expected_version=state["version"],
        checkout_path=str(checkout),
        doc_type="plan",
        mode="state",
        ops=[
            {
                "op": "append_evidence",
                "plan": PLAN,
                "anchor": anchor,
                "title": f"{anchor} evidence",
                "body": body,
            }
        ],
    )


def _insert_sections(checkout: Path, markup: str) -> None:
    path = _record_path(checkout)
    text = path.read_text(encoding="utf-8")
    boundary = text.index("</main>")
    path.write_text(text[:boundary] + markup + "\n" + text[boundary:], encoding="utf-8")


def _tags(result) -> dict[str, str]:
    return {anchor: tag for tag, anchor in result.lost_anchors}


def _section(text: str, element_id: str):
    return _soup(text).find("section", id=element_id)


def _soup(text: str) -> BeautifulSoup:
    return BeautifulSoup(text, "html.parser")


def _rewritten_record(checkout: Path) -> Path:
    """A record an earlier synthesis wrote, with appends and legacy sections."""
    _synthesize(checkout)
    assert (
        _append(
            checkout, "x-appended", "<p>Appended evidence from an earlier landing.</p>"
        )["ok"]
        is True
    )
    assert (
        _append(checkout, "x-dropped", "<p>Evidence a later run supersedes.</p>")["ok"]
        is True
    )
    _insert_sections(
        checkout,
        '<section id="legacy-anchor"><h2>Legacy anchor</h2>'
        "<p>Written before the mark existed.</p></section>\n"
        '<section id="s3"><h2>Third outcome</h2>'
        "<p>Generated when s3 still carried a comment.</p></section>",
    )
    # A run lands on the id an append had taken, so this synthesis renders it.
    ledger.append_run(
        PROJECT,
        _run_record("run-later", "x-dropped", "2026-11-07T12:00:00Z", ("ccccccc",)),
        root=checkout,
    )
    return _record_path(checkout)


def test_a_marked_append_is_carried_with_its_text(checkout: Path) -> None:
    _rewritten_record(checkout)

    result = _synthesize(checkout)
    text = result.path.read_text(encoding="utf-8")
    carried = _section(text, "x-appended")

    assert carried is not None, text
    assert carried.get(evidence.APPEND_ORIGIN_ATTRIBUTE) == evidence.APPEND_ORIGIN_VALUE
    assert "Appended evidence from an earlier landing." in carried.get_text()
    assert "x-appended" not in _tags(result)


def test_the_synthesis_writes_its_own_sections_once(checkout: Path) -> None:
    _rewritten_record(checkout)

    result = _synthesize(checkout)
    soup = _soup(result.path.read_text(encoding="utf-8"))

    assert len(soup.find_all("section", id="delivery")) == 1
    # The carried append is written once, and the run's own section once.
    assert len(soup.find_all("section", id="x-appended")) == 1
    assert len(soup.find_all("section", id="x-dropped")) == 1


def test_a_marked_anchor_a_run_now_renders_is_dropped_and_named(
    checkout: Path,
) -> None:
    _rewritten_record(checkout)

    result = _synthesize(checkout)
    text = result.path.read_text(encoding="utf-8")

    assert _tags(result)["x-dropped"] == "dropped"
    rendered_section = _section(text, "x-dropped")
    assert rendered_section is not None
    assert rendered_section.get(evidence.APPEND_ORIGIN_ATTRIBUTE) is None
    assert "run-later" in rendered_section.get_text()


def test_the_command_names_every_lost_anchor_it_dropped(checkout: Path) -> None:
    _rewritten_record(checkout)

    cli = CliRunner().invoke(
        main,
        [
            "evidence",
            "synthesize",
            "--project",
            PROJECT,
            "--plan",
            PLAN,
            "--checkout-path",
            str(checkout),
        ],
    )

    assert cli.exit_code == 0, cli.output
    assert "dropped: x-dropped" in cli.output
    assert "carried: legacy-anchor" in cli.output
    assert "retired: s3" in cli.output


def test_an_unmarked_section_on_a_rendered_id_is_replaced(checkout: Path) -> None:
    _rewritten_record(checkout)

    result = _synthesize(checkout)

    assert _tags(result)["verification"] == "replaced"


def test_an_unmarked_anchor_outside_the_reserved_set_is_carried(checkout: Path) -> None:
    _rewritten_record(checkout)

    result = _synthesize(checkout)
    text = result.path.read_text(encoding="utf-8")

    assert _tags(result)["legacy-anchor"] == "carried"
    assert "Written before the mark existed." in text


def test_an_unrendered_plan_section_is_retired_not_carried(checkout: Path) -> None:
    _rewritten_record(checkout)

    result = _synthesize(checkout)
    text = result.path.read_text(encoding="utf-8")

    assert _tags(result)["s3"] == "retired"
    assert "Generated when s3 still carried a comment." not in text


def test_an_append_on_a_plan_section_id_is_refused(checkout: Path) -> None:
    _synthesize(checkout)

    for anchor in ("s4", "s5.1"):  # s5.1 normalises to the plan's s5-1
        result = _append(checkout, anchor, "<p>Should never land.</p>")
        assert result["ok"] is False, (anchor, result)
    assert "Should never land." not in _record_path(checkout).read_text(
        encoding="utf-8"
    )


def test_an_append_on_an_id_only_a_ledger_run_renders_is_refused(
    checkout: Path,
) -> None:
    _synthesize(checkout)
    ledger.append_run(
        PROJECT,
        _run_record("run-runtime", "s7-runtime", "2026-11-07T13:00:00Z", ("ddddddd",)),
        root=checkout,
    )

    result = _append(checkout, "s7-runtime", "<p>Should never land.</p>")

    assert result["ok"] is False, result


def test_the_synthesis_and_the_refusal_read_one_renderable_set(
    checkout: Path,
) -> None:
    # A key no plan section and no run carries: the append is allowed, and the
    # synthesis carries the section without rendering a section of its own.
    assert (
        _append(checkout, "comment-only-beat", "<p>Early evidence.</p>")["ok"] is True
    )
    result = _synthesize(checkout)
    carried = _section(result.path.read_text(encoding="utf-8"), "comment-only-beat")
    assert carried.get(evidence.APPEND_ORIGIN_ATTRIBUTE) == evidence.APPEND_ORIGIN_VALUE
    assert "Early evidence." in carried.get_text()

    # The plan gains a comment on that key, so the record renders it.
    _write_plan(
        checkout,
        comments={
            "comment-only-beat": [
                {
                    "id": "comment-beat",
                    "who": "worker-a",
                    "when": "2026-11-07T14:00:00Z",
                    "body": "<p>The beat landed.</p>",
                }
            ]
        },
    )

    result = _synthesize(checkout)
    rendered = _section(result.path.read_text(encoding="utf-8"), "comment-only-beat")
    assert rendered is not None
    assert "The beat landed." in rendered.get_text()

    refused = _append(checkout, "comment-only-beat", "<p>Should never land.</p>")
    assert refused["ok"] is False, refused


def test_control_dropping_the_carry_loses_the_appended_text(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declared mutation: dropping the appended-section carry loses case one."""
    _rewritten_record(checkout)

    monkeypatch.setattr(evidence, "_carried_sections", lambda *_a, **_k: ([], []))
    result = _synthesize(checkout)
    text = result.path.read_text(encoding="utf-8")

    assert "Appended evidence from an earlier landing." not in text
    assert _section(text, "x-appended") is None
