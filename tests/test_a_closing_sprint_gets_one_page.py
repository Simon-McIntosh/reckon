"""Closing a sprint writes one page with a row per member plan.

The page is the sprint's record. It is read from the same section reading each
member plan's closure overview renders, so a row's counts are the counts that
plan's overview states, and it links only documents and figures that exist: a
plan with no overview is listed with its status and with no link to a record
that was never written.
"""

from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from reckon import _plan_html, doccheck, evidence, ledger
from reckon.project_state import (
    apply_resource_ops,
    create_project_state,
    read_resource,
    write_resource,
)

PROJECT = "sample"
SPRINT = "S1"
ALPHA = "alpha-plan"
BETA = "beta-plan"
GAMMA = "gamma-plan"

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
ALPHA_FIGURE = f"docs/figures/{ALPHA}/s1-node/attempts.svg"
FIGURE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="80">'
    "<title>Attempts</title>"
    '<rect width="120" height="80" fill="#ffffff"/>'
    "</svg>"
)


def _authored(slug: str, sections: tuple[str, ...], done: tuple[str, ...]) -> str:
    body = "".join(
        f'<h2 id="{section_id}">§{index} — {section_id} delivery</h2>'
        f"<p>{section_id} sets out to do its own work.</p>"
        + (
            f"<p><strong>Done when</strong> {section_id} shows its gate passes.</p>"
            if section_id in done
            else ""
        )
        for index, section_id in enumerate(sections, start=1)
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f"<title>{slug}</title></head><body>"
        '<main class="plan-doc">'
        f"{body}"
        "</main></body></html>"
    )


def _state(slug: str, sections: tuple[str, ...], done: tuple[str, ...]) -> dict:
    declarations = {
        section_id: ("done" if section_id in done else "implementable")
        for section_id in sections
    }
    return {
        "project": PROJECT,
        "type": "plan",
        "slug": slug,
        "title": slug.replace("-", " ").title(),
        "status": "done",
        "modified": "2026-11-07",
        "version": 1,
        "effort_hours": float(len(sections)),
        "section_declarations": declarations,
        "sections": [
            {
                "id": section_id,
                "effort_hours": 1.0,
                "capability": deepcopy(CAPABILITY),
                "attempts": 0,
                "status": declarations[section_id],
                "links": [],
            }
            for section_id in sections
        ],
        "followups": [],
        "comments": {
            section_id: [
                {
                    "id": f"c-close-{section_id}",
                    "who": "reckon-build",
                    "when": "2026-11-07T12:00:00Z",
                    "body": f"<p>{section_id} landed.</p>",
                }
            ]
            for section_id in done
        },
    }


def _write_plan(
    root: Path,
    slug: str,
    sections: tuple[str, ...],
    done: tuple[str, ...],
    *,
    unlanded: tuple[str, ...] = (),
) -> None:
    path = root / "docs" / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    state = _state(slug, sections, done)
    for section in unlanded:
        del state["comments"][section]
    path.write_text(
        _plan_html.write_state(_authored(slug, sections, done), state),
        encoding="utf-8",
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
    plan: str,
    run_id: str,
    section: str,
    node: str,
    completed_at: str,
    *,
    manifest: Path | None = None,
) -> dict:
    return ledger.build_record(
        run_id=run_id,
        plan=plan,
        section=section,
        node=node,
        role="implement",
        gate="passed",
        completed_at=completed_at,
        completed_at_source="provided",
        worker_seconds=600,
        commits=("aaaaaaa",),
        tests_added=1,
        changed_lines={"insertions": 9, "deletions": 2},
        manifest_path=str(manifest or ""),
        gate_check={"command": GATE_COMMAND, "exit_status": 0, "log_path": ""},
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
                "failure_count": 0,
                "failure_ids": [],
                "log_path": "runs/head.log",
            },
            "added_failure_ids": [],
        },
    )


def _build_repo(root: Path) -> Path:
    docs = root / "docs"
    create_project_state(docs, PROJECT)
    _write_plan(root, ALPHA, ("s1", "s2", "s3"), ("s1", "s2", "s3"), unlanded=("s3",))
    _write_plan(root, BETA, ("s1",), ("s1",))
    _write_plan(root, GAMMA, ("s1",), ())
    figure = docs / "figures" / ALPHA / "s1-node" / "attempts.svg"
    figure.parent.mkdir(parents=True, exist_ok=True)
    figure.write_text(FIGURE_SVG, encoding="utf-8")
    ledger.write(PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=root)
    alpha_manifest = _write_manifest(
        root, "alpha-s1-node", figures=f'figures: ["{ALPHA_FIGURE}"]\n'
    )
    (root / "docs" / "figures" / ALPHA / "s1-node").mkdir(parents=True, exist_ok=True)
    ledger.append_run(
        PROJECT,
        _run_record(
            ALPHA,
            "alpha-s1",
            "s1",
            "alpha-s1-node",
            "2026-11-07T10:00:00Z",
            manifest=alpha_manifest,
        ),
        root=root,
    )
    ledger.append_run(
        PROJECT,
        _run_record(ALPHA, "alpha-s2", "s2", "alpha-s2-node", "2026-11-07T10:30:00Z"),
        root=root,
    )
    ledger.append_run(
        PROJECT,
        _run_record(BETA, "beta-s1", "s1", "beta-s1-node", "2026-11-07T11:00:00Z"),
        root=root,
    )
    write_resource(
        docs,
        PROJECT,
        "sprint",
        SPRINT,
        {"status": "active", "items": [ALPHA, BETA, GAMMA]},
        0,
        create=True,
    )
    return root


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    return _build_repo(tmp_path / "repo")


def _close_sprint(root: Path) -> Path:
    docs = root / "docs"
    _record, version = read_resource(docs, PROJECT, "sprint", SPRINT)
    apply_resource_ops(
        docs,
        PROJECT,
        "sprint",
        SPRINT,
        [{"op": "set", "path": "status", "value": "done"}],
        version,
    )
    return evidence.sprint_page_path(docs, SPRINT)


def _page(root: Path) -> BeautifulSoup:
    page = _close_sprint(root)
    assert page.is_file(), f"the close wrote no page at {page}"
    return BeautifulSoup(page.read_text(encoding="utf-8"), "html.parser")


def _rows(soup: BeautifulSoup) -> list[dict[str, str]]:
    """Each member-plan row, keyed by the column it fills."""
    table = soup.find("section", id="sprint-plans").find("table")
    headings = [
        cell.get_text(" ", strip=True) for cell in table.find("thead").find_all("th")
    ]
    return [
        dict(
            zip(
                headings,
                (cell.get_text(" ", strip=True) for cell in row.find_all("td")),
                strict=False,
            )
        )
        for row in table.find("tbody").find_all("tr")
    ]


def _overview_counts(root: Path, slug: str) -> tuple[int, int]:
    """The runs and landed sections one plan's closure overview states."""
    evidence.synthesize_landed_record(root / "docs", PROJECT, slug)
    record = root / "docs" / "evidence" / "archive" / f"{slug}-landed.html"
    soup = BeautifulSoup(record.read_text(encoding="utf-8"), "html.parser")
    paragraph = soup.find("p", class_="overview-totals")
    assert paragraph is not None, f"no overview totals in {record}"
    text = paragraph.get_text(" ")
    match = re.search(r"(\d+) run\(s\) across (\d+) landed section\(s\)", text)
    assert match, text
    return int(match.group(1)), int(match.group(2))


def _resolve(root: Path, url: str) -> Path | None:
    """The repository file a page address names, or None for a non-repo link."""
    url = url.strip()
    if not url or url.startswith(("http://", "https://", "#", "mailto:")):
        return None
    prefix = f"/{PROJECT}/"
    assert url.startswith(prefix), f"{url} is not addressed on the project mount"
    return root / "docs" / url[len(prefix) :]


def _links(soup: BeautifulSoup) -> list[str]:
    return [node["href"] for node in soup.find_all("a")] + [
        node["src"] for node in soup.find_all("img")
    ]


def test_the_page_has_a_row_per_member_plan_with_its_overview_counts(
    checkout: Path,
) -> None:
    rows = _rows(_page(checkout))
    assert len(rows) == 3, rows
    by_plan = {row["Plan"]: row for row in rows}
    assert set(by_plan) == {"Alpha Plan", "Beta Plan", "Gamma Plan"}, by_plan

    for slug, title in ((ALPHA, "Alpha Plan"), (BETA, "Beta Plan")):
        runs, landed = _overview_counts(checkout, slug)
        assert int(by_plan[title]["Runs"]) == runs, (title, by_plan[title])
        assert int(by_plan[title]["Sections landed"]) == landed, (title, by_plan[title])


def test_a_done_section_without_a_landing_does_not_inflate_the_row(
    checkout: Path,
) -> None:
    plan = _plan_html.parse_plan(checkout / "docs" / "plans" / f"{ALPHA}.html")
    assert list(plan["section_declarations"].values()) == ["done"] * 3
    assert not plan["comments"].get("s3")
    records = ledger.runs(PROJECT, root=checkout, plan=ALPHA)
    assert {record["section"] for record in records} == {"s1", "s2"}
    row = next(row for row in _rows(_page(checkout)) if row["Plan"] == "Alpha Plan")
    assert int(row["Sections landed"]) == 2, row


def test_a_member_with_no_document_is_listed_without_a_link(checkout: Path) -> None:
    docs = checkout / "docs"
    sprint, _version = read_resource(docs, PROJECT, "sprint", SPRINT)
    (docs / "plans" / f"{GAMMA}.html").unlink()
    path = evidence.write_sprint_page(
        docs, PROJECT, SPRINT, [item["slug"] for item in sprint["items"]]
    )
    page = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    rows = page.find("section", id="sprint-plans").find("tbody").find_all("tr")
    missing = next(row for row in rows if row.find("td").get_text() == GAMMA)
    assert missing.find_all("td")[1].get_text() == "no plan document"
    assert missing.find("a") is None, missing
    present = next(row for row in rows if row.find("td").get_text() == "Alpha Plan")
    assert present.find("a") is not None, "a member with a document stays linked"


def test_a_plan_with_no_overview_is_listed_with_its_status_and_no_broken_link(
    checkout: Path,
) -> None:
    page = _page(checkout)
    rows = {row["Plan"]: row for row in _rows(page)}
    assert "Gamma Plan" in rows, rows
    assert rows["Gamma Plan"]["Status"] == "done", rows["Gamma Plan"]
    gamma_cell = next(
        row.find("td")
        for row in page.find("section", id="sprint-plans").find("tbody").find_all("tr")
        if "Gamma Plan" in row.find("td").get_text()
    )
    assert gamma_cell.find("a") is not None, "the existing plan document is linked"
    assert not (
        checkout / "docs" / "evidence" / "archive" / f"{GAMMA}-landed.html"
    ).exists()
    resolved = [_resolve(checkout, anchor["href"]) for anchor in page.find_all("a")]
    missing = [
        url
        for url in _links(page)
        if _resolve(checkout, url) is not None and not _resolve(checkout, url).is_file()
    ]
    assert missing == [], missing
    assert resolved


def test_every_href_and_src_on_the_page_resolves_to_a_file(checkout: Path) -> None:
    page = _page(checkout)
    links = _links(page)
    assert links, "the page links something"
    assert any(link.endswith(".svg") for link in links), links
    missing = [
        url
        for url in links
        if _resolve(checkout, url) is None or not _resolve(checkout, url).is_file()
    ]
    assert missing == [], missing


def test_the_page_passes_audit_doc(checkout: Path) -> None:
    page_path = _close_sprint(checkout)
    findings = doccheck.audit_html(
        page_path.read_text(encoding="utf-8"), project=PROJECT
    )
    errors = [finding.fmt() for finding in findings if finding.severity == "error"]
    assert errors == [], errors
