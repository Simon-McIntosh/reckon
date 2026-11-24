"""A close composition that fails for any reason still leaves a record.

Closing a plan composes its evidence report once, after the plan file is
written. The composition can fail for reasons the store does not name, and the
close has already been decided by then, so the plan must stay closed while the
failure is kept: a durable record naming the plan and the error, and the
close's own result saying no report was composed. A composition that succeeds
leaves its report exactly as before.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from reckon import _plan_html, evidence, ledger
from reckon.mcp_edit_plan import _edit_plan_tool

PROJECT = "sample"
PLAN = "demo-plan"
TITLE = "Demo plan"

HEADINGS = ("s1", "s2")
NODE = "s1-node"

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


def _state() -> dict:
    declarations = dict.fromkeys(HEADINGS, "done")
    return {
        "project": PROJECT,
        "type": "plan",
        "slug": PLAN,
        "title": TITLE,
        "status": "active",
        "modified": "2026-11-24",
        "version": 0,
        "effort_hours": 3.0,
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
                "title": "close the plan",
                "body": "why",
                "written_by": "reckon-build",
                "written_at": "2026-11-24",
                "prompt": f"/reckon-build {PLAN} §2",
            }
        ],
        "comments": {},
    }


def _plan_file(root: Path) -> Path:
    return root / "docs" / "plans" / f"{PLAN}.html"


def _record_path(root: Path) -> Path:
    return root / "docs" / "evidence" / "archive" / f"{PLAN}-landed.html"


def _write_frame(root: Path) -> None:
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


def _write_manifest(root: Path) -> Path:
    path = root / "run" / NODE / "manifest.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"node: {NODE}\n"
        'orientation_write_paths: ["reckon/_store.py"]\n'
        "status: complete\n",
        encoding="utf-8",
    )
    return path


def _run_record(manifest_path: str) -> dict:
    return ledger.build_record(
        run_id=f"{NODE}-run",
        plan=PLAN,
        section="s1",
        node=NODE,
        role="implement",
        gate="passed",
        completed_at="2026-11-24T10:00:00Z",
        completed_at_source="provided",
        worker_seconds=600,
        commits=("abc1234",),
        tests_added=2,
        changed_lines={"insertions": 9, "deletions": 2},
        manifest_path=manifest_path,
        gate_check={
            "command": GATE_COMMAND,
            "exit_status": 0,
            "log_path": f"runs/{NODE}-run/gate.log",
        },
    )


def _build_repo(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    path = _plan_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _plan_html.write_state(_authored(), _state()), encoding="utf-8"
    )
    ledger.write(PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=root)
    manifest = _write_manifest(root)
    ledger.append_run(
        PROJECT, _run_record(str(manifest)), root=root
    )
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8")
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    return root


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return _build_repo(tmp_path / "repo", tmp_path, monkeypatch)


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


def _stored_status(root: Path) -> str:
    soup = BeautifulSoup(_plan_file(root).read_text(encoding="utf-8"), "html.parser")
    meta = soup.find("meta", attrs={"name": "plan-status"})
    assert meta is not None, "the plan carries no status metadata"
    return meta["content"]


def _failure_record() -> dict:
    path = evidence.close_failure_path(PROJECT, PLAN)
    assert path.is_file(), f"no close-failure record at {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _unexpected_failure(*_args, **_kwargs):
    raise RuntimeError("the ledger is unreadable")


def test_an_unexpected_composition_failure_leaves_the_plan_closed_and_recorded(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "reckon.evidence.synthesize_landed_record", _unexpected_failure
    )

    result = _close(repo)

    # The close is the record of a decision already taken: it stands.
    assert result.get("ok") is True, json.dumps(result, indent=2)
    assert _stored_status(repo) == "done"

    # The failure itself is kept, naming the plan and the error.
    record = _failure_record()
    assert record["plan"] == PLAN, record
    assert record["project"] == PROJECT, record
    assert record["error_type"] == "RuntimeError", record
    assert "the ledger is unreadable" in record["error"], record


def test_the_close_result_says_no_report_was_composed(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "reckon.evidence.synthesize_landed_record", _unexpected_failure
    )

    result = _close(repo)

    assert result.get("ok") is True, json.dumps(result, indent=2)
    warnings = result.get("warnings") or []
    assert any(
        "report was not composed" in message and PLAN in message
        for message in warnings
    ), warnings


def test_a_healthy_composition_still_writes_its_report(repo: Path) -> None:
    result = _close(repo)

    assert result.get("ok") is True, json.dumps(result, indent=2)
    assert _stored_status(repo) == "done"
    assert _record_path(repo).is_file(), "the close composed no report"
    assert "report was not composed" not in " ".join(result.get("warnings") or [])
    assert not evidence.close_failure_path(PROJECT, PLAN).exists(), (
        "a healthy composition left a failure record"
    )
