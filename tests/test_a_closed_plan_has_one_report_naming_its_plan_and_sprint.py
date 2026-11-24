"""A closed plan keeps one report naming the plan and the sprint it ran in.

The report is composed once, when the plan reaches a terminal status, and it
lives where the plan's landed record already lives. It names the plan and its
sprint in its head metadata, links both from its first block, and embeds each
node's figures beneath that node's row. A landing on a plan that stays open
composes nothing, and a plan belonging to no sprint says so rather than leaving
the statement out.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from click.testing import CliRunner

from reckon import _plan_html, ledger
from reckon.cli import main
from reckon.mcp_edit_plan import _edit_plan_tool

PROJECT = "sample"
PLAN = "demo-plan"
TITLE = "Demo plan"
SPRINT = "S9"

HEADINGS = ("s1", "s2", "s3")
NODES = {"s1": "s1-node", "s2": "s2-node", "s3": "s3-node"}
FIGURES = {
    "s1-node": f"docs/figures/{PLAN}/s1-node/attempts.svg",
    "s2-node": f"docs/figures/{PLAN}/s2-node/timeline.svg",
}

CAPABILITY = {
    "version": "1.0",
    "class": "general",
    "requirements": {"reasoning": "standard", "verification": "strict", "risk": "low"},
}

GATE_COMMAND = "PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_demo.py"


def _authored() -> str:
    body = "".join(
        f'<h2 id="{section_id}">§{index} — {section_id} delivery</h2>'
        f"<p>{section_id} sets out to do its own work.</p>"
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


def _state(*, sprint: str | None) -> dict:
    declarations = dict.fromkeys(HEADINGS, "done")
    state = {
        "project": PROJECT,
        "type": "plan",
        "slug": PLAN,
        "title": TITLE,
        "status": "active",
        "modified": "2026-11-23",
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
        "followups": [
            {
                "id": "f1",
                "status": "open",
                "title": "land the last section",
                "body": "why",
                "written_by": "reckon-build",
                "written_at": "2026-11-23",
                "prompt": f"/reckon-build {PLAN} §3",
            }
        ],
        "comments": {},
    }
    if sprint is not None:
        state["sprint"] = sprint
    return state


def _plan_file(root: Path) -> Path:
    return root / "docs" / "plans" / f"{PLAN}.html"


def _write_plan(root: Path, *, sprint: str | None) -> Path:
    path = _plan_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _plan_html.write_state(_authored(), _state(sprint=sprint)), encoding="utf-8"
    )
    return path


def _write_sprint(root: Path) -> Path:
    path = root / "docs" / "sprints" / f"{SPRINT}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{SPRINT}</title></head><body><main></main></body></html>",
        encoding="utf-8",
    )
    return path


def _write_frame(root: Path) -> Path:
    """The empty evidence frame a close requires before it runs its guards."""
    path = _record_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}-landed</title></head>"
        '<body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(
        _plan_html.write_state(
            bare,
            {
                "version": 0,
                "type": "evidence",
                "evidence_for": [PLAN],
                "status": "active",
            },
        ),
        encoding="utf-8",
    )
    return path


def _record_path(root: Path) -> Path:
    return root / "docs" / "evidence" / "archive" / f"{PLAN}-landed.html"


def _write_manifest(root: Path, node: str, figures: tuple[str, ...] | None) -> Path:
    path = root / "run" / node / "manifest.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    figure_lines = ""
    if figures:
        joined = ", ".join(f'"{figure}"' for figure in figures)
        figure_lines = f"figures: [{joined}]\n"
    path.write_text(
        f"node: {node}\n"
        'orientation_write_paths: ["reckon/_store.py"]\n'
        f"{figure_lines}"
        "status: complete\n",
        encoding="utf-8",
    )
    return path


def _run_record(
    run_id: str, section: str, node: str, completed_at: str, *, manifest_path: str
) -> dict:
    return ledger.build_record(
        run_id=run_id,
        plan=PLAN,
        section=section,
        node=node,
        role="implement",
        gate="passed",
        completed_at=completed_at,
        completed_at_source="provided",
        worker_seconds=600,
        commits=(run_id.replace("-", "")[:7].ljust(7, "0"),),
        tests_added=2,
        changed_lines={"insertions": 9, "deletions": 2},
        manifest_path=manifest_path,
        gate_check={
            "command": GATE_COMMAND,
            "exit_status": 0,
            "log_path": f"runs/{run_id}/gate.log",
        },
    )


def _build_repo(root: Path, *, sprint: str | None) -> Path:
    _write_plan(root, sprint=sprint)
    if sprint is not None:
        _write_sprint(root)
    ledger.write(PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=root)
    for section_id in HEADINGS:
        node = NODES[section_id]
        figures = (FIGURES[node],) if node in FIGURES else None
        manifest = _write_manifest(root, node, figures)
        ledger.append_run(
            PROJECT,
            _run_record(
                f"{node}-run",
                section_id,
                node,
                "2026-11-23T10:00:00Z",
                manifest_path=str(manifest),
            ),
            root=root,
        )
    return root


def _fixture_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, sprint: str | None
) -> Path:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    root = _build_repo(tmp_path / "repo", sprint=sprint)
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8")
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    return root


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return _fixture_repo(tmp_path, monkeypatch, sprint=SPRINT)


@pytest.fixture()
def no_sprint_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return _fixture_repo(tmp_path, monkeypatch, sprint=None)


def _close(root: Path) -> dict:
    from reckon import _store

    _write_frame(root)
    _data, version = _store.read_plan(PROJECT, PLAN, str(root))
    return _edit_plan_tool(
        PROJECT,
        PLAN,
        expected_version=version,
        ops=[
            {
                "op": "close",
                "status": "done",
                "outcome": "every section landed — no followup",
                "by": "reckon-build",
            }
        ],
        checkout_path=str(root),
    )


def _record(root: Path) -> BeautifulSoup:
    return BeautifulSoup(_record_path(root).read_text(encoding="utf-8"), "html.parser")


def _first_block(soup: BeautifulSoup) -> str:
    main = soup.find("main")
    return main.find("h1").find_next("p", recursive=False).get_text(" ")


def _evidence_files(root: Path) -> list[Path]:
    return sorted((root / "docs" / "evidence").rglob(f"{PLAN}-landed.html"))


def test_closing_a_plan_composes_one_report_naming_the_plan_and_sprint(
    repo: Path,
) -> None:
    result = _close(repo)

    assert result.get("ok") is True, json.dumps(result, indent=2)
    assert len(_evidence_files(repo)) == 1, _evidence_files(repo)
    soup = _record(repo)

    sprint_meta = soup.find("meta", attrs={"name": "plan-sprint"})
    assert sprint_meta is not None, "the report's metadata names no sprint"
    assert sprint_meta["content"] == SPRINT, sprint_meta["content"]
    slug_meta = soup.find("meta", attrs={"name": "plan-slug"})
    assert slug_meta["content"] == f"{PLAN}-landed", slug_meta["content"]

    first = soup.find("main").find("h1").find_next("p", recursive=False)
    assert first.find("a", href=f"/{PROJECT}/plans/{PLAN}.html") is not None, first
    assert first.find("a", href=f"/{PROJECT}/sprints/{SPRINT}.html") is not None, first

    nodes_section = soup.find("section", id="landed-nodes")
    for node, figure_path in FIGURES.items():
        group = nodes_section.find(
            "div", class_="node-figures", attrs={"data-node": node}
        )
        assert group is not None, f"no figure group for {node}"
        image = group.find("img")
        assert image is not None, f"no figure beneath {node}'s row"
        assert image["src"] == f"/{PROJECT}/{figure_path[len('docs/') :]}", image["src"]

    cli = CliRunner().invoke(main, ["audit-doc", str(_record_path(repo))])
    assert cli.exit_code == 0, cli.output


def _append_comment(root: Path) -> dict:
    from reckon import _store

    _data, version = _store.read_plan(PROJECT, PLAN, str(root))
    return _edit_plan_tool(
        PROJECT,
        PLAN,
        expected_version=version,
        ops=[
            {
                "op": "append",
                "target": "comments",
                "section": "s1",
                "item": {
                    "id": "c-run-s1-node-run",
                    "who": "reckon-build",
                    "when": "2026-11-23T10:00:00Z",
                    "body": "<p>A node landed; the plan stays open.</p>",
                },
            }
        ],
        checkout_path=str(root),
    )


def test_no_landing_composes_a_report_for_an_open_plan(repo: Path) -> None:
    _write_frame(repo)
    frame = _record_path(repo).read_text(encoding="utf-8")

    result = _append_comment(repo)

    assert result.get("ok") is True, json.dumps(result, indent=2)
    assert _record_path(repo).read_text(encoding="utf-8") == frame, (
        "a landing on an open plan rewrote the record"
    )


def test_no_landing_on_an_open_plan_creates_no_report(repo: Path) -> None:
    assert _evidence_files(repo) == [], "the fixture started with a record"

    result = _append_comment(repo)

    assert result.get("ok") is True, json.dumps(result, indent=2)
    assert _evidence_files(repo) == [], (
        "a landing on an open plan composed a report where none existed"
    )


def test_both_links_are_derived_from_the_one_address_rule(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reckon import evidence

    original = evidence._served_href
    monkeypatch.setattr(
        evidence,
        "_served_href",
        lambda project, resource: original(project, resource).replace(
            f"/{PROJECT}/", "/marked/"
        ),
    )

    result = _close(repo)

    assert result.get("ok") is True, json.dumps(result, indent=2)
    first = _record(repo).find("main").find("h1").find_next("p", recursive=False)
    plan_link = first.find("a", href="/marked/plans/demo-plan.html")
    sprint_link = first.find("a", href=f"/marked/sprints/{SPRINT}.html")
    assert plan_link is not None, first
    assert sprint_link is not None, (
        "the sprint link does not come from the record's one address rule"
    )


def test_closing_a_plan_in_no_sprint_states_none(no_sprint_repo: Path) -> None:
    result = _close(no_sprint_repo)

    assert result.get("ok") is True, json.dumps(result, indent=2)
    soup = _record(no_sprint_repo)
    sprint_meta = soup.find("meta", attrs={"name": "plan-sprint"})
    assert sprint_meta is not None, "the report's metadata names no sprint"
    assert sprint_meta["content"] == "none", sprint_meta["content"]
    first = _first_block(soup)
    assert "no sprint" in first, first
