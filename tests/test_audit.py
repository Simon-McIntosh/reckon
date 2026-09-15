from __future__ import annotations

import json
import os
import time
from pathlib import Path

from click.testing import CliRunner

from reckon._plan_html import write_state
from reckon.cli import main


def _write_plan(docs_dir: Path, slug: str, state: dict) -> Path:
    bare = (
        '<!doctype html><html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="docs-project" content="proj">'
        f'<title>{slug}</title></head>'
        '<body><main class="plan-doc"></main></body></html>'
    )
    path = docs_dir / f"{slug}.html"
    path.write_text(write_state(bare, state), encoding="utf-8")
    return path


def _age_file(path: Path, *, days: int) -> None:
    stamp = time.time() - (days * 86400)
    os.utime(path, (stamp, stamp))


def test_audit_flags_stale_missing_impl_and_stale_rca(tmp_path, monkeypatch):
    project = "proj"
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps({project: str(docs_dir)}), encoding="utf-8")
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))

    stale = _write_plan(
        docs_dir,
        "stale-plan",
        {"slug": "stale-plan", "title": "Stale Plan", "status": "active", "impl": 0.5},
    )
    missing_impl = _write_plan(
        docs_dir,
        "missing-impl",
        {"slug": "missing-impl", "title": "Missing Impl", "status": "shipped"},
    )
    stale_rca = _write_plan(
        docs_dir,
        "stale-rca",
        {"slug": "stale-rca", "title": "Stale RCA", "type": "research", "status": "active"},
    )
    clean = _write_plan(
        docs_dir,
        "clean-plan",
        {"slug": "clean-plan", "title": "Clean Plan", "status": "done", "impl": 1.0},
    )

    _age_file(stale, days=31)
    _age_file(missing_impl, days=5)
    _age_file(stale_rca, days=61)
    _age_file(clean, days=3)

    result = CliRunner().invoke(main, ["audit"])

    assert result.exit_code == 1
    assert "project" in result.output
    assert "stale-plan" in result.output
    assert "STALE" in result.output
    assert "missing-impl" in result.output
    assert "MISSING_IMPL" in result.output
    assert "stale-rca" in result.output
    assert "STALE_RCA" in result.output
    assert "clean-plan" not in result.output


def test_audit_project_filter_limits_output(tmp_path, monkeypatch):
    docs_a = tmp_path / "docs-a"
    docs_b = tmp_path / "docs-b"
    docs_a.mkdir()
    docs_b.mkdir()
    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(
        json.dumps({"proj-a": str(docs_a), "proj-b": str(docs_b)}),
        encoding="utf-8",
    )
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))

    stale_a = _write_plan(
        docs_a,
        "stale-a",
        {"slug": "stale-a", "title": "Stale A", "status": "active", "impl": 0.2},
    )
    stale_b = _write_plan(
        docs_b,
        "stale-b",
        {"slug": "stale-b", "title": "Stale B", "status": "active", "impl": 0.3},
    )
    _age_file(stale_a, days=31)
    _age_file(stale_b, days=31)

    result = CliRunner().invoke(main, ["audit", "--project", "proj-b"])

    assert result.exit_code == 0
    assert "stale-b" in result.output
    assert "proj-b" in result.output
    assert "stale-a" not in result.output
    assert "proj-a" not in result.output


def _open_followup(followup_id: str, **kwargs) -> dict:
    return {
        "id": followup_id,
        "status": "open",
        "recommends_skill": "",
        "prompt": "",
        "title": "Followup",
        "body": "Body",
        **kwargs,
    }


def _mounts(tmp_path, monkeypatch, mounts: dict) -> None:
    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps(mounts), encoding="utf-8")
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))


def test_audit_reports_followup_to_foreign_terminal_plan(tmp_path, monkeypatch):
    from reckon.doccheck import (
        FOLLOWUP_FOREIGN_TERMINAL,
        ForeignFollowupFinding,
        audit_lifecycle,
    )

    docs_a = tmp_path / "docs-a"
    docs_b = tmp_path / "docs-b"
    docs_a.mkdir()
    docs_b.mkdir()
    _mounts(tmp_path, monkeypatch, {"proj-a": str(docs_a), "proj-b": str(docs_b)})
    _write_plan(
        docs_a,
        "holder",
        {
            "slug": "holder",
            "title": "Holder",
            "status": "active",
            "impl": 0.5,
            "followups": [
                _open_followup(
                    "f1",
                    recommends_skill="/reckon-ship proj-b:foreign-terminal",
                    prompt="/reckon-ship proj-b:foreign-terminal",
                )
            ],
        },
    )
    _write_plan(
        docs_b,
        "foreign-terminal",
        {
            "slug": "foreign-terminal",
            "title": "Shipped Elsewhere",
            "status": "shipped",
            "impl": 1.0,
        },
    )

    followup_findings = [
        item for item in audit_lifecycle() if isinstance(item, ForeignFollowupFinding)
    ]
    assert len(followup_findings) == 1
    finding = followup_findings[0]
    assert finding.flag == FOLLOWUP_FOREIGN_TERMINAL
    assert finding.project == "proj-a"
    assert finding.slug == "holder"  # the holding plan
    assert finding.target_project == "proj-b"
    assert finding.target_slug == "foreign-terminal"  # the foreign target
    assert finding.target_status == "shipped"

    result = CliRunner().invoke(main, ["audit"])
    assert result.exit_code == 0
    assert "FOLLOWUP_FOREIGN_TERMINAL" in result.output
    assert "holder" in result.output
    assert result.output.count("FOLLOWUP_FOREIGN_TERMINAL") == 1


def test_audit_silent_for_followup_to_live_foreign_plan(tmp_path, monkeypatch):
    from reckon.doccheck import ForeignFollowupFinding, audit_lifecycle

    docs_a = tmp_path / "docs-a"
    docs_b = tmp_path / "docs-b"
    docs_a.mkdir()
    docs_b.mkdir()
    _mounts(tmp_path, monkeypatch, {"proj-a": str(docs_a), "proj-b": str(docs_b)})
    _write_plan(
        docs_a,
        "holder",
        {
            "slug": "holder",
            "title": "Holder",
            "status": "active",
            "impl": 0.5,
            "followups": [
                _open_followup("f1", recommends_skill="/reckon-ship proj-b:live-plan")
            ],
        },
    )
    _write_plan(
        docs_b,
        "live-plan",
        {
            "slug": "live-plan",
            "title": "Live Elsewhere",
            "status": "active",
            "impl": 0.5,
        },
    )

    followup_findings = [
        item for item in audit_lifecycle() if isinstance(item, ForeignFollowupFinding)
    ]
    assert followup_findings == []
    result = CliRunner().invoke(main, ["audit"])
    assert "FOLLOWUP_FOREIGN" not in result.output


def test_audit_reports_unmounted_foreign_target_distinctly(tmp_path, monkeypatch):
    from reckon.doccheck import (
        FOLLOWUP_FOREIGN_UNMOUNTED,
        ForeignFollowupFinding,
        audit_lifecycle,
    )

    docs_a = tmp_path / "docs-a"
    docs_a.mkdir()
    _mounts(tmp_path, monkeypatch, {"proj-a": str(docs_a)})
    _write_plan(
        docs_a,
        "holder",
        {
            "slug": "holder",
            "title": "Holder",
            "status": "active",
            "impl": 0.5,
            "followups": [
                _open_followup("f1", recommends_skill="/reckon-ship proj-c:elsewhere")
            ],
        },
    )

    followup_findings = [
        item for item in audit_lifecycle() if isinstance(item, ForeignFollowupFinding)
    ]
    assert len(followup_findings) == 1
    finding = followup_findings[0]
    assert finding.flag == FOLLOWUP_FOREIGN_UNMOUNTED
    assert finding.target_project == "proj-c"
    assert finding.target_slug == "elsewhere"

    result = CliRunner().invoke(main, ["audit"])
    assert "FOLLOWUP_FOREIGN_UNMOUNTED" in result.output
    assert "FOLLOWUP_FOREIGN_TERMINAL" not in result.output


def test_audit_followup_finding_count_over_registered_mounts(tmp_path, monkeypatch):
    from reckon.doccheck import (
        FOLLOWUP_FOREIGN_TERMINAL,
        ForeignFollowupFinding,
        audit_lifecycle,
    )

    docs_a = tmp_path / "docs-a"
    docs_b = tmp_path / "docs-b"
    docs_a.mkdir()
    docs_b.mkdir()
    _mounts(tmp_path, monkeypatch, {"proj-a": str(docs_a), "proj-b": str(docs_b)})
    for project, docs, holder_slug in (
        ("proj-a", docs_a, "holder-one"),
        ("proj-b", docs_b, "holder-two"),
    ):
        target_project = "proj-a" if project == "proj-b" else "proj-b"
        _write_plan(
            docs,
            holder_slug,
            {
                "slug": holder_slug,
                "title": holder_slug,
                "status": "active",
                "impl": 0.5,
                "followups": [
                    _open_followup(
                        f"f-{holder_slug}",
                        recommends_skill=f"/reckon-ship {target_project}:terminator",
                    )
                ],
            },
        )
    for docs in (docs_a, docs_b):
        _write_plan(
            docs,
            "terminator",
            {"slug": "terminator", "title": "Done", "status": "done", "impl": 1.0},
        )

    followup_findings = [
        item for item in audit_lifecycle() if isinstance(item, ForeignFollowupFinding)
    ]
    assert len(followup_findings) == 2
    assert all(item.flag == FOLLOWUP_FOREIGN_TERMINAL for item in followup_findings)

    result = CliRunner().invoke(main, ["audit"])
    assert result.output.count("FOLLOWUP_FOREIGN") == 2


def test_audit_ignores_local_and_resolved_followup_refs(tmp_path, monkeypatch):
    from reckon.doccheck import ForeignFollowupFinding, audit_lifecycle

    docs_a = tmp_path / "docs-a"
    docs_b = tmp_path / "docs-b"
    docs_a.mkdir()
    docs_b.mkdir()
    _mounts(tmp_path, monkeypatch, {"proj-a": str(docs_a), "proj-b": str(docs_b)})
    _write_plan(
        docs_a,
        "holder",
        {
            "slug": "holder",
            "title": "Holder",
            "status": "active",
            "impl": 0.5,
            "followups": [
                # Local ref (no project qualifier) — never foreign.
                _open_followup("local", recommends_skill="/reckon-ship a-local-plan"),
                # Resolved followup naming a foreign terminal plan — not pending.
                {
                    **_open_followup(
                        "resolved",
                        recommends_skill="/reckon-ship proj-b:foreign-terminal",
                    ),
                    "resolved_at": "2026-09-01T00:00:00+00:00",
                    "resolved_by": "agent",
                    "outcome": "done",
                },
                # Same-project-qualified ref — reads as local, not foreign.
                _open_followup(
                    "self-qualified", recommends_skill="/reckon-ship proj-a:holder"
                ),
            ],
        },
    )
    _write_plan(
        docs_b,
        "foreign-terminal",
        {"slug": "foreign-terminal", "title": "Done", "status": "done", "impl": 1.0},
    )

    followup_findings = [
        item for item in audit_lifecycle() if isinstance(item, ForeignFollowupFinding)
    ]
    assert followup_findings == []
