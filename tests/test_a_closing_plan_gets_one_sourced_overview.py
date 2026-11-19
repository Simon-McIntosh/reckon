"""A closing plan's overview: one paragraph per section, every claim sourced.

The landed record a close requires is a reading of what each section landed —
its landing comment, its runs and the figures those runs named. The checks
below build a temporary repository with a ledger, one figure and a
three-section plan of two landed sections, and hold the rendered record to the
overview contract: a paragraph per section, a source on every factual sentence,
one row per landed node however many attempts it took, the gate figures a run
record holds, the unlanded section's declaration, the figure with its caption,
and no fragment text read into any of it.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from click.testing import CliRunner

from reckon import _plan_html, evidence, ledger
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

GATE_COMMAND = "PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_demo.py"
FIGURE_PATH = f"docs/figures/{PLAN}/s1-node/attempts.svg"
FRAGMENT_MARKER = "FRAGMENT-ONLY-MARKER-9f3"

S1_LANDED = "Section one landed; the reader now reads one record."
S2_LANDED = "Section two landed; the figure renders from the plan mount."

HEADINGS = ("s1", "s2", "s3")

DONE_WHEN_PATH = f"docs/evidence/archive/{PLAN}-landed.html"
REVIEW_A = "r-20261107T120000000000-plan-review-of-demo-plan"
REVIEW_B = "r-20261107T123000000000-plan-review-of-demo-plan"
FOLD_REASON = "carried into the node that owned the path"
EXEMPT_REASON = "outside this plan's remit"
PROMOTION_LANDED = "Section one landed by the promotion of its landed run."
REVIEW_NOTE = "Reviewed the landed run and raised what this record holds."


def _authored() -> str:
    body = "".join(
        f'<h2 id="{section_id}">§{index} — {section_id} delivery</h2>'
        f"<p>{section_id} sets out to do its own work.</p>"
        f"<p><strong>Done when</strong> {section_id} shows <code>reckon audit-doc</code>"
        f" passes on {DONE_WHEN_PATH}.</p>"
        for index, section_id in enumerate(HEADINGS, start=1)
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f"<title>{TITLE}</title></head><body>"
        '<main class="plan-doc">'
        f"{body}"
        "</main></body></html>"
    )


def _state(comments: dict | None = None) -> dict:
    declarations = {"s1": "done", "s2": "done", "s3": "implementable"}
    return {
        "project": PROJECT,
        "type": "plan",
        "slug": PLAN,
        "title": TITLE,
        "status": "active",
        "modified": "2026-11-07",
        "version": 0,
        "effort_hours": 4.5,
        "section_declarations": declarations,
        "sections": [
            {
                "id": section_id,
                "effort_hours": 1.5,
                "capability": deepcopy(CAPABILITY),
                "attempts": 0,
                "status": declarations[section_id],
                "links": [],
            }
            for section_id in HEADINGS
        ],
        "followups": [],
        "comments": dict(comments or {}),
    }


def _landing_comments() -> dict:
    return {
        "s1": [
            {
                "id": "c-close-s1",
                "who": "reckon-build",
                "when": "2026-11-07T12:00:00Z",
                "body": f"<p>{S1_LANDED}</p>",
            }
        ],
        "s2": [
            {
                "id": "c-close-s2",
                "who": "reckon-build",
                "when": "2026-11-07T12:30:00Z",
                "body": f"<p>{S2_LANDED}</p>",
            }
        ],
    }


def _plan_file(root: Path) -> Path:
    return root / "docs" / "plans" / f"{PLAN}.html"


def _write_plan(root: Path, comments: dict) -> None:
    path = _plan_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _plan_html.write_state(_authored(), _state(comments)),
        encoding="utf-8",
    )


def _write_review(root: Path, review_run_id: str, record: dict) -> Path:
    """Store one committed plan review where the project's reader looks for it."""
    path = (
        root
        / "docs"
        / "state"
        / PROJECT
        / "reviews"
        / "plan"
        / PLAN
        / f"{review_run_id}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"project": PROJECT, "plan_slug": PLAN, **record}, indent=2),
        encoding="utf-8",
    )
    return path


def _review_records() -> tuple[tuple[str, dict], ...]:
    """Two stored reviews: one folded a finding twice, one exempted a finding."""
    return (
        (
            REVIEW_A,
            {
                "review_run_id": REVIEW_A,
                "status": "ready",
                "findings": [
                    {"id": f"f{index}", "text": "a finding"} for index in range(3)
                ],
                "finding_dispositions": {
                    "0": {"kind": "folded", "node": "s1-node"},
                    "1": {"kind": "folded", "node": "s2-node"},
                },
            },
        ),
        (
            REVIEW_B,
            {
                "review_run_id": REVIEW_B,
                "status": "ready",
                "findings": [
                    {"id": f"g{index}", "text": "a finding"} for index in range(2)
                ],
                "finding_dispositions": {
                    "0": {"kind": "exempted", "reason": EXEMPT_REASON}
                },
            },
        ),
    )


def _write_manifest(root: Path, node: str, figures: str = "") -> Path:
    path = root / "run" / node / "manifest.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"node: {node}\n"
        'orientation_write_paths: ["reckon/_store.py"]\n'
        f"{figures}"
        "status: complete\n",
        encoding="utf-8",
    )
    return path


def _run_record(
    run_id: str,
    section: str,
    node: str,
    completed_at: str,
    *,
    commits: tuple[str, ...] = ("aaaaaaa",),
    manifest: Path | None = None,
    gate: str = "passed",
) -> dict:
    return ledger.build_record(
        run_id=run_id,
        plan=PLAN,
        section=section,
        node=node,
        role="implement",
        gate=gate,
        completed_at=completed_at,
        completed_at_source="provided",
        worker_seconds=600,
        commits=commits,
        tests_added=2,
        changed_lines={"insertions": 9, "deletions": 2},
        manifest_path=str(manifest or ""),
        gate_check={
            "command": GATE_COMMAND,
            "exit_status": 0,
            "log_path": f"runs/{run_id}/gate.log",
        },
        suite_delta={
            "status": "clean",
            "suite_command": GATE_COMMAND,
            "baseline_suite": {
                "revision": "base000",
                "command": GATE_COMMAND,
                "exit_status": 0,
                "completed": True,
                "failure_count": 0,
                "failure_ids": [],
                "log_path": "runs/base.log",
            },
            "after_suite": {
                "revision": "head000",
                "command": GATE_COMMAND,
                "exit_status": 0,
                "completed": True,
                "failure_count": 1,
                "failure_ids": ["tests/test_demo.py::test_known"],
                "log_path": "runs/head.log",
            },
            "added_failure_ids": [],
        },
    )


def _promotion_comments() -> dict:
    """s1 landed by promotion only; s2 carries a review's comment last."""
    return {
        "s1": [
            {
                "id": "c-run-s1-attempt-3",
                "who": "reckon-build",
                "when": "2026-11-07T11:00:00Z",
                "body": f"<p>{PROMOTION_LANDED}</p>",
            },
            {
                "id": f"c-run-{REVIEW_B}",
                "who": "reckon-build",
                "when": "2026-11-07T12:45:00Z",
                "body": f"<p>{REVIEW_NOTE}</p>",
            },
        ],
        "s2": [
            {
                "id": "c-close-s2",
                "who": "reckon-build",
                "when": "2026-11-07T12:30:00Z",
                "body": f"<p>{S2_LANDED}</p>",
            },
            {
                "id": f"c-run-{REVIEW_A}",
                "who": "reckon-build",
                "when": "2026-11-07T13:00:00Z",
                "body": f"<p>{REVIEW_NOTE}</p>",
            },
        ],
    }


def _build_repo(root: Path, comments: dict) -> Path:
    _write_plan(root, comments)
    ledger.write(PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=root)
    for review_run_id, record in _review_records():
        _write_review(root, review_run_id, record)
    latest = _write_manifest(root, "s1-node", figures=f'figures: ["{FIGURE_PATH}"]\n')
    _write_manifest(root, "s2-node")
    for attempt, stamp in enumerate(
        ("2026-11-07T10:00:00Z", "2026-11-07T10:30:00Z"), start=1
    ):
        ledger.append_run(
            PROJECT,
            _run_record(
                f"s1-attempt-{attempt}",
                "s1",
                "s1-node",
                stamp,
                commits=(f"a{attempt}aaaaa",),
            ),
            root=root,
        )
    ledger.append_run(
        PROJECT,
        _run_record(
            "s1-attempt-3",
            "s1",
            "s1-node",
            "2026-11-07T11:00:00Z",
            manifest=latest,
        ),
        root=root,
    )
    ledger.append_run(
        PROJECT,
        _run_record(
            "s2-run", "s2", "s2-node", "2026-11-07T11:30:00Z", commits=("bbbbbbb",)
        ),
        root=root,
    )
    return root


@pytest.fixture()
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    return _build_repo(tmp_path / "repo", _landing_comments())


@pytest.fixture()
def promoted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    return _build_repo(tmp_path / "repo", _promotion_comments())


def _record_path(root: Path) -> Path:
    return root / "docs" / "evidence" / "archive" / f"{PLAN}-landed.html"


def _synthesize(root: Path):
    return evidence.synthesize_landed_record(root / "docs", PROJECT, PLAN)


def _soup(root: Path) -> BeautifulSoup:
    return BeautifulSoup(_record_path(root).read_text(encoding="utf-8"), "html.parser")


def _paragraph_texts(soup: BeautifulSoup) -> dict[str, str]:
    return {
        p["data-section"]: p.get_text(" ") for p in soup.select("p.section-summary")
    }


def check_one_paragraph_per_section(soup: BeautifulSoup) -> None:
    paragraphs = _paragraph_texts(soup)
    assert set(paragraphs) == set(HEADINGS), paragraphs
    assert S1_LANDED in paragraphs["s1"], paragraphs["s1"]
    assert S2_LANDED in paragraphs["s2"], paragraphs["s2"]


OVERVIEW_BLOCKS = ("overview", "landed-nodes", "figures", "totals")


def check_every_sentence_is_sourced(soup: BeautifulSoup) -> None:
    """Every sentence the overview states cites the record it rests on.

    The check is scoped to the overview's own blocks: the anchored sections
    beneath it are the plan's comment and run record, which carry their run ids
    and stamps as content rather than as a claim the overview makes.
    """
    main = soup.find("main")
    scopes = [main.find("h1").find_next("p", recursive=False)]
    for block in OVERVIEW_BLOCKS:
        scope = main.find("section", id=block)
        scopes.extend(scope.find_all("p"))
    unsourced = [
        paragraph.get_text(" ").strip()
        for paragraph in scopes
        if paragraph is not None
        and paragraph.find(class_=evidence.SOURCE_CLASS) is None
    ]
    assert unsourced == [], unsourced


def check_one_row_per_landed_node(soup: BeautifulSoup) -> None:
    nodes_table = soup.find("section", id="landed-nodes").find("table")
    rows = nodes_table.find_all("tr")[1:]
    nodes = [row.find("td").get_text(strip=True) for row in rows]
    assert nodes == ["s1-node", "s2-node"], nodes
    attempts = {
        row.find_all("td")[0].get_text(strip=True): row.find_all("td")[2].get_text(
            strip=True
        )
        for row in rows
    }
    assert attempts == {"s1-node": "3 attempts", "s2-node": "1 attempt"}, attempts


def check_gate_figures_carry_their_run(soup: BeautifulSoup) -> None:
    paragraph = soup.find("p", attrs={"data-section": "s1"})
    text = paragraph.get_text(" ")
    assert GATE_COMMAND in text, text
    assert "exited 0" in text, text
    assert "2 tests added" in text, text
    assert "1 suite failures" in text, text
    sources = {span["data-source"] for span in paragraph.find_all(class_="source")}
    assert "s1-attempt-3" in sources, sources


def check_unlanded_section(soup: BeautifulSoup) -> None:
    paragraphs = _paragraph_texts(soup)
    assert "s3" in paragraphs, sorted(paragraphs)
    text = paragraphs["s3"]
    assert "declared implementable" in text, text
    assert "nothing landed" in text, text
    assert S1_LANDED not in text, text
    paragraph = soup.find("p", attrs={"data-section": "s3"})
    assert paragraph.find(class_=evidence.SOURCE_CLASS) is not None


def check_figure_with_its_caption(soup: BeautifulSoup) -> None:
    figure = soup.find("figure")
    assert figure is not None
    image = figure.find("img")
    assert image["src"] == f"/{PROJECT}/figures/{PLAN}/s1-node/attempts.svg", image[
        "src"
    ]
    assert "attempts.svg" in figure.find("figcaption").get_text(" ")


def check_promotion_landing(soup: BeautifulSoup) -> None:
    """A section landed by promotion quotes the landing comment of the landed run."""
    text = _paragraph_texts(soup)["s1"]
    assert PROMOTION_LANDED in text, text
    assert REVIEW_NOTE not in text, text
    sources = {
        span["data-source"]
        for span in soup.find("p", attrs={"data-section": "s1"}).find_all(
            class_=evidence.SOURCE_CLASS
        )
    }
    assert "c-run-s1-attempt-3" in sources, sources


def check_a_review_comment_is_not_the_landing(soup: BeautifulSoup) -> None:
    """A trailing review comment is never quoted as the section's landing.

    The section read here holds two comments and no close comment: the landing
    comment of the run that landed it, and a review of that run written after
    it. Reading the last comment therefore reads the review, which is the
    mistake this case exists to catch. The close-comment section proves the
    other half: a close comment wins even with a review written after it.
    """
    paragraph = soup.find("p", attrs={"data-section": "s1"})
    text = paragraph.get_text(" ")
    assert PROMOTION_LANDED in text, text
    assert REVIEW_NOTE not in text, text
    sources = {
        span["data-source"] for span in paragraph.find_all(class_=evidence.SOURCE_CLASS)
    }
    assert f"c-run-{REVIEW_B}" not in sources, sources
    close_paragraph = _paragraph_texts(soup)["s2"]
    assert S2_LANDED in close_paragraph, close_paragraph
    assert REVIEW_NOTE not in close_paragraph, close_paragraph


def check_the_whole_done_when_is_quoted(soup: BeautifulSoup) -> None:
    """A done-when holding an abbreviation and a dotted path is quoted whole."""
    paragraph = soup.find("p", attrs={"data-section": "s1"})
    quoted = paragraph.find("q", class_="done-when").get_text(" ")
    assert "reckon audit-doc" in quoted, quoted
    assert DONE_WHEN_PATH in quoted, quoted


def check_review_totals(soup: BeautifulSoup) -> None:
    """Review findings and their dispositions appear, cited to their reviews."""
    paragraph = soup.find("p", class_="overview-totals")
    text = paragraph.get_text(" ")
    assert "5 finding(s) raised" in text, text
    assert "2 folded" in text, text
    assert "1 exempted" in text, text
    assert "2 open" in text, text
    assert REVIEW_A in text, text
    assert REVIEW_B in text, text
    sources = {
        span["data-source"] for span in paragraph.find_all(class_=evidence.SOURCE_CLASS)
    }
    assert {REVIEW_A, REVIEW_B} <= sources, sources


def check_no_fragment_text(root: Path) -> None:
    assert FRAGMENT_MARKER not in _record_path(root).read_text(encoding="utf-8")


def check_audit_doc(root: Path) -> None:
    result = CliRunner().invoke(main, ["audit-doc", str(_record_path(root))])
    assert result.exit_code == 0, result.output


def test_the_overview_carries_one_paragraph_per_section(checkout: Path) -> None:
    _synthesize(checkout)

    check_one_paragraph_per_section(_soup(checkout))


def test_no_factual_sentence_lacks_a_source(checkout: Path) -> None:
    _synthesize(checkout)

    check_every_sentence_is_sourced(_soup(checkout))


def test_a_node_with_three_attempts_renders_one_row(checkout: Path) -> None:
    _synthesize(checkout)

    check_one_row_per_landed_node(_soup(checkout))


def test_the_gate_figures_appear_with_their_run(checkout: Path) -> None:
    _synthesize(checkout)

    check_gate_figures_carry_their_run(_soup(checkout))


def test_the_unlanded_section_states_unchanged_declaration(checkout: Path) -> None:
    _synthesize(checkout)

    check_unlanded_section(_soup(checkout))


def test_the_named_figure_renders_with_its_caption(checkout: Path) -> None:
    _synthesize(checkout)

    check_figure_with_its_caption(_soup(checkout))


def test_a_fragment_is_not_read_into_the_overview(checkout: Path) -> None:
    fragment = checkout / "docs" / "evidence" / "fragments" / PLAN / "s1-node.html"
    fragment.parent.mkdir(parents=True, exist_ok=True)
    fragment.write_text(
        f"<html><body><main><p>{FRAGMENT_MARKER}</p></main></body></html>",
        encoding="utf-8",
    )

    _synthesize(checkout)

    check_no_fragment_text(checkout)


def test_the_overview_passes_the_doc_audit(checkout: Path) -> None:
    _synthesize(checkout)

    check_every_sentence_is_sourced(_soup(checkout))
    check_audit_doc(checkout)


def test_a_rerun_writes_the_overview_once(checkout: Path) -> None:
    _synthesize(checkout)
    _synthesize(checkout)

    soup = _soup(checkout)
    assert len(soup.find_all("section", id="overview")) == 1
    assert len(soup.find_all("section", id="s1")) == 1


def test_a_section_landed_by_promotion_quotes_its_run_comment(
    promoted: Path,
) -> None:
    _synthesize(promoted)

    check_promotion_landing(_soup(promoted))


def test_a_reviews_last_comment_is_not_the_landing(promoted: Path) -> None:
    _synthesize(promoted)

    check_a_review_comment_is_not_the_landing(_soup(promoted))


def test_the_done_when_is_quoted_whole(checkout: Path) -> None:
    _synthesize(checkout)

    check_the_whole_done_when_is_quoted(_soup(checkout))


def test_a_body_with_no_paragraph_yields_no_done_when() -> None:
    """A body with no block boundary is not quoted: its whole text would be."""
    source = (
        '<!doctype html><html lang="en"><body><main class="plan-doc">'
        '<h2 id="s1">§1 — s1 delivery</h2>'
        "<div>s1 sets out to do its own work. Done when s1 holds a record.</div>"
        "</main></body></html>"
    )

    bodies = evidence._section_texts(source)

    assert evidence._done_when(bodies["s1"]) == "", bodies["s1"]


def test_the_review_findings_are_totalled_by_disposition(checkout: Path) -> None:
    _synthesize(checkout)

    check_review_totals(_soup(checkout))


def test_control_no_source_element_fails_the_sourced_case(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declared mutation: a renderer that emits a sentence with no source."""
    monkeypatch.setattr(evidence, "_source", lambda *_args, **_kwargs: "")
    _synthesize(checkout)

    with pytest.raises(AssertionError):
        check_every_sentence_is_sourced(_soup(checkout))


def test_control_one_row_per_run_fails_the_attempt_case(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def one_row_per_run(landings, **_kwargs) -> list[str]:
        return [
            "      <tr>"
            f"<td>{record.get('node')}</td><td>{landing.section}</td>"
            "<td>1 attempts</td>"
            f"<td>{record.get('gate')}</td>"
            "<td>—</td><td>—</td><td>—</td></tr>"
            for landing in landings
            for record in landing.runs
        ]

    monkeypatch.setattr(evidence, "_node_rows", one_row_per_run)
    _synthesize(checkout)

    with pytest.raises(AssertionError):
        check_one_row_per_landed_node(_soup(checkout))


def test_control_omitting_the_run_figures_fails_the_figure_case(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evidence, "_figure_blocks", lambda *_args, **_kwargs: [])
    _synthesize(checkout)

    with pytest.raises(AssertionError):
        check_figure_with_its_caption(_soup(checkout))


def test_control_the_last_comment_fallback_fails_the_promotion_case(
    promoted: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declared mutation: the landing read from whichever comment came last."""

    def last_comment(comments, runs=()):
        for comment in comments:
            if str(comment.get("id") or "").startswith("c-close-"):
                return comment
        return comments[-1] if comments else {}

    monkeypatch.setattr(evidence, "_landing_comment", last_comment)
    _synthesize(promoted)

    with pytest.raises(AssertionError):
        check_promotion_landing(_soup(promoted))


def test_control_skipping_the_unlanded_section_fails_its_case(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def landed_only(plan, source, records, project="", **kwargs):
        return [
            landing
            for landing in _read_all(plan, source, records, project)
            if landing.landed
        ]

    _read_all = evidence.read_section_landings
    monkeypatch.setattr(evidence, "read_section_landings", landed_only)
    _synthesize(checkout)

    with pytest.raises(AssertionError):
        check_unlanded_section(_soup(checkout))
