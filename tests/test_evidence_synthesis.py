from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html, ledger
from reckon.cli import main
from reckon.doccheck import audit_html
from reckon.evidence import EvidenceSynthesisError, synthesize_landed_record


PROJECT = "proj"
PLAN = "closure-record"


def _write_plan(docs: Path) -> Path:
    bare = (
        '<!doctype html><html lang="en"><head>'
        '<meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<title>Closure record</title></head><body><main class="plan-doc">'
        '<h2 id="delivery">Delivery outcome</h2>'
        '<h2 id="verification">Verification outcome</h2>'
        "</main></body></html>"
    )
    state = {
        "type": "plan",
        "slug": PLAN,
        "title": "Closure Record",
        "status": "active",
        "comments": {
            "delivery": [
                {
                    "id": "comment-delivery",
                    "who": "worker-a",
                    "when": "2026-08-24T19:00:00Z",
                    "body": "<p>The implementation removed the duplicate write.</p>",
                }
            ],
            "verification": [
                {
                    "id": "comment-verification",
                    "who": "worker-b",
                    "when": "2026-08-24T19:05:00Z",
                    "body": "<p>The negative path stayed visible and reproducible.</p>",
                }
            ],
        },
    }
    path = docs / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")
    return path


def _record(
    run_id: str,
    *,
    section: str,
    commits: tuple[str, ...],
    gate: str,
    completed_at: str,
    plan: str = PLAN,
) -> dict:
    return ledger.build_record(
        run_id=run_id,
        plan=plan,
        section=section,
        node=f"{section}-node",
        gate=gate,
        completed_at=completed_at,
        completed_at_source="provided",
        worker_seconds=95,
        commits=commits,
        tests_added=2,
        changed_lines={"insertions": 14, "deletions": 3},
    )


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    root = tmp_path / "repo"
    docs = root / "docs"
    (docs / "state" / PROJECT).mkdir(parents=True)
    _write_plan(docs)
    ledger.append_run(
        PROJECT,
        _record(
            "run-delivery",
            section="delivery",
            commits=("abc1234", "def5678"),
            gate="passed",
            completed_at="2026-08-24T19:02:00Z",
        ),
        root=root,
    )
    ledger.append_run(
        PROJECT,
        _record(
            "run-verification",
            section="verification",
            commits=("987fedc",),
            gate="not-run",
            completed_at="2026-08-24T19:06:00Z",
        ),
        root=root,
    )
    ledger.append_run(
        PROJECT,
        _record(
            "run-other-plan",
            section="delivery",
            commits=("other111",),
            gate="failed",
            completed_at="2026-08-24T19:07:00Z",
            plan="unrelated",
        ),
        root=root,
    )
    return root


def test_synthesis_joins_section_comments_to_only_the_plans_runs(
    repository: Path,
) -> None:
    result = synthesize_landed_record(repository / "docs", PROJECT, PLAN)
    output = result.path.read_text(encoding="utf-8")

    assert result.runs == 2
    assert result.comments == 2
    assert '<section id="delivery">' in output
    assert '<section id="verification">' in output
    assert "The implementation removed the duplicate write." in output
    assert "The negative path stayed visible and reproducible." in output
    assert "run-delivery" in output
    assert "run-verification" in output
    assert "run-other-plan" not in output
    assert "other111" not in output


def test_every_landed_commit_and_gate_is_in_a_valid_evidence_document(
    repository: Path,
) -> None:
    result = synthesize_landed_record(repository / "docs", PROJECT, PLAN)
    output = result.path.read_text(encoding="utf-8")

    for commit in ("abc1234", "def5678", "987fedc"):
        assert commit in output
    assert "passed" in output
    assert "not-run" in output
    assert f'<meta name="plan-evidence-for" content="{PLAN}">' in output
    assert [
        finding for finding in audit_html(output) if finding.severity == "error"
    ] == []
    audit = CliRunner().invoke(main, ["audit-doc", str(result.path)])
    assert audit.exit_code == 0, audit.output


def test_cli_registers_the_synthesis_and_writes_the_canonical_path(
    repository: Path,
) -> None:
    result = CliRunner().invoke(
        main,
        [
            "evidence",
            "synthesize",
            "--project",
            PROJECT,
            "--plan",
            PLAN,
            "--checkout-path",
            str(repository),
        ],
    )

    expected = repository / "docs" / "evidence" / "archive" / f"{PLAN}-landed.html"
    assert result.exit_code == 0, result.output
    assert str(expected) in result.output
    assert "from 2 run(s), 2 comment(s), and 3 commit(s)" in result.output
    assert expected.is_file()


def test_plan_without_landed_runs_is_refused_without_creating_a_record(
    repository: Path,
) -> None:
    docs = repository / "docs"
    empty_plan = "no-completions"
    source = _write_plan(docs).read_text(encoding="utf-8")
    empty_path = docs / "plans" / f"{empty_plan}.html"
    empty_path.write_text(
        _plan_html.write_state(source, {"slug": empty_plan, "title": "No completions"}),
        encoding="utf-8",
    )

    with pytest.raises(EvidenceSynthesisError, match="no committed landed runs"):
        synthesize_landed_record(docs, PROJECT, empty_plan)

    assert not (docs / "evidence" / "archive" / f"{empty_plan}-landed.html").exists()


def test_repeated_synthesis_is_byte_identical(repository: Path) -> None:
    first = synthesize_landed_record(repository / "docs", PROJECT, PLAN)
    first_bytes = first.path.read_bytes()
    second = synthesize_landed_record(repository / "docs", PROJECT, PLAN)

    assert second.path == first.path
    assert second.path.read_bytes() == first_bytes
