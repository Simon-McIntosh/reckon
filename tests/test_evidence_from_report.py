"""A commit-less run composes its landed record from the report it delivered.

The ledger row is the composer's long-standing source, and an investigate
node's deliverable is a report rather than a diff, so its row holds no commits
to render. These cases pin the report-backed source the composer needs in
order to compose such a plan without a hand-authored evidence file.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import reckon.evidence as evidence_module
from reckon import _plan_html, ledger
from reckon.doccheck import audit_html
from reckon.evidence import synthesize_landed_record

PROJECT = "proj"
PLAN = "investigated-closure"
REPORT_BODY = (
    "verdict: the option is absent from the surface\n\n"
    "Detail: none of the six flags changes the command that runs.\n"
)


def _write_plan(docs: Path) -> Path:
    bare = (
        '<!doctype html><html lang="en"><head>'
        '<meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        "<title>Investigated closure</title></head><body>"
        '<main class="plan-doc">'
        '<h2 id="delivery">Delivery outcome</h2>'
        '<h2 id="verification">Verification outcome</h2>'
        "</main></body></html>"
    )
    state = {
        "type": "plan",
        "slug": PLAN,
        "title": "Investigated Closure",
        "status": "active",
        "comments": {
            "verification": [
                {
                    "id": "comment-verification",
                    "who": "worker-a",
                    "when": "2026-08-24T19:05:00Z",
                    "body": "<p>The investigation ran rather than the mutation.</p>",
                }
            ]
        },
    }
    path = docs / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")
    return path


def _repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a throwaway checkout holding the plan and its project state."""

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    root = tmp_path / "repo"
    docs = root / "docs"
    (docs / "state" / PROJECT).mkdir(parents=True, exist_ok=True)
    ledger.write(PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=root)
    _write_plan(docs)
    return root


def _delivered_report(base: Path, run_id: str, body: str) -> tuple[Path, Path]:
    """Write a report and the manifest naming it, returning both paths."""

    report = base / "reports" / f"{run_id}.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(body, encoding="utf-8")
    manifest = base / "runs" / run_id / "manifest.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"orientation_worktree: {base}\n"
        f"orientation_base_sha: {'0' * 40}\n"
        f'orientation_write_paths: ["{report}"]\n'
        f"node: {run_id}\n"
        "status: complete\n"
        f'artifacts: ["{report}"]\n',
        encoding="utf-8",
    )
    return manifest, report


def _commitless_record(run_id: str, section: str, manifest: Path) -> dict:
    """Build one investigate node's row, whose deliverable is a report."""

    return ledger.build_record(
        run_id=run_id,
        plan=PLAN,
        section=section,
        node=run_id,
        role="investigate",
        gate="passed",
        completed_at="2026-08-24T19:02:00Z",
        completed_at_source="provided",
        worker_seconds=120,
        commits=(),
        manifest_path=str(manifest),
    )


def _section_slice(document: str, section_id: str) -> str:
    opening = f'<section id="{section_id}">'
    start = document.index(opening)
    end = document.index("</section>", start)
    return document[start:end]


def test_a_commitless_run_is_cited_from_its_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repository(tmp_path, monkeypatch)
    manifest, report = _delivered_report(tmp_path, "run-investigate", REPORT_BODY)
    ledger.append_run(
        PROJECT,
        _commitless_record("run-investigate", "verification", manifest),
        root=root,
    )

    result = synthesize_landed_record(root / "docs", PROJECT, PLAN)
    output = result.path.read_text(encoding="utf-8")
    section = _section_slice(output, "verification")

    assert '<section id="verification">' in output
    assert "run-investigate" in section
    assert str(report) in section
    assert f'data-report-path="{report}"' in section
    assert "the option is absent from the surface" in section
    assert "none of the six flags changes the command that runs" in section
    assert result.runs == 1
    assert result.commits == 0
    assert [
        finding for finding in audit_html(output) if finding.severity == "error"
    ] == []


def test_a_commitless_run_without_a_readable_report_states_the_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repository(tmp_path, monkeypatch)
    manifest, report = _delivered_report(tmp_path, "run-empty", REPORT_BODY)
    report.unlink()
    assert not report.exists()
    ledger.append_run(
        PROJECT,
        _commitless_record("run-empty", "verification", manifest),
        root=root,
    )

    result = synthesize_landed_record(root / "docs", PROJECT, PLAN)
    output = result.path.read_text(encoding="utf-8")
    section = _section_slice(output, "verification")

    assert "run-empty" in section
    assert "landed-report-unreadable" in section
    assert "no record composed" in section
    assert "landed no commits" in section
    assert "no section could be composed from a report" in section
    assert str(report) in section
    assert section.strip() != ""


def test_the_ledger_row_still_composes_a_run_that_has_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repository(tmp_path, monkeypatch)
    manifest, _report = _delivered_report(
        tmp_path, "run-delivery", "CONTENT THAT MUST NOT BE CITED"
    )
    ledger.append_run(
        PROJECT,
        ledger.build_record(
            run_id="run-delivery",
            plan=PLAN,
            section="delivery",
            node="delivery-node",
            gate="passed",
            completed_at="2026-08-24T19:02:00Z",
            completed_at_source="provided",
            worker_seconds=95,
            commits=("abc1234", "def5678"),
            tests_added=2,
            changed_lines={"insertions": 14, "deletions": 3},
            manifest_path=str(manifest),
        ),
        root=root,
    )

    result = synthesize_landed_record(root / "docs", PROJECT, PLAN)
    output = result.path.read_text(encoding="utf-8")
    section = _section_slice(output, "delivery")

    assert "<code>abc1234</code>" in section
    assert "<code>def5678</code>" in section
    assert "<strong>passed</strong>" in section
    assert result.commits == 2
    assert "landed-report" not in section
    assert "CONTENT THAT MUST NOT BE CITED" not in output


def test_evidence_module_defines_one_document_renderer() -> None:
    source = Path(evidence_module.__file__).read_text(encoding="utf-8")
    renderers = [
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(inner, ast.Constant)
            and isinstance(inner.value, str)
            and inner.value.lstrip().startswith("<!doctype html")
            for inner in ast.walk(node)
        )
    ]

    assert renderers == ["_render_document"]
