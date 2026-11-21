"""A landing finishes when the plan it lands on was edited after its snapshot."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from reckon import _plan_html, ledger
from reckon.crew import promotion
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git as _git
from tests.helpers.landing_repository import (
    PLAN,
    PROJECT,
    _complete_land,
    _write_origin_and_checkout,
)

MARKER = "the reviewed change survives the plan edit\n"
RUN_ID = "r-20261120T074437034953-wlsr-plan-edit"
PLAN_PATH = f"docs/plans/{PLAN}.html"
SECTION = "s7"


def _plan_document(*, paragraph: str, with_section: bool = True) -> str:
    section = (
        f'<h2 id="{SECTION}">Section seven</h2>'
        f'<section data-reckon="section" data-id="{SECTION}" '
        'data-effort-hours="1.0" data-capability-version="1.0" '
        'data-capability-class="general" data-capability-reasoning="standard" '
        'data-capability-verification="strict" '
        'data-capability-risk="moderate" data-status="implementable" '
        'data-links=""></section>'
        if with_section
        else ""
    )
    return (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        f'</head><body><main class="plan-doc">{section}'
        f"<p>{paragraph}</p></main></body></html>\n"
    )


def _write_plan(repository: Path, document: str) -> None:
    (repository / PLAN_PATH).write_text(
        _plan_html.write_state(
            document,
            {
                "type": "plan",
                "slug": PLAN,
                "title": "One call lands a run",
                "status": "active",
                "version": 0,
                "comments": {},
            },
        ),
        encoding="utf-8",
    )


def _commit_plan(repository: Path, message: str) -> None:
    _git(repository, "add", "--", PLAN_PATH)
    _git(repository, "commit", "-q", "-m", message)
    _git(repository, "push", "-q", "origin", "main")


def _is_ancestor(repository: Path, revision: str, branch: str) -> bool:
    return (
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "merge-base",
                "--is-ancestor",
                revision,
                branch,
            ],
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )


@pytest.fixture()
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A finished run whose head carries one committed change, ready to land."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    origin, repository = _write_origin_and_checkout(tmp_path)
    _write_plan(repository, _plan_document(paragraph="The authored text."))
    _commit_plan(repository, "docs: author the delivery section")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    worktree = tmp_path / "worktrees" / "plan-edit"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    (worktree / "reviewed.txt").write_text(MARKER, encoding="utf-8")
    _git(worktree, "add", "reviewed.txt")
    _git(worktree, "commit", "-q", "-m", "add the reviewed change")
    head = _git(worktree, "rev-parse", "HEAD")
    manifest = tmp_path / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("node: node-a\nstatus: complete\n", encoding="utf-8")
    gate_log = tmp_path / "gate.log"
    gate_log.write_text("# command: true\nEXIT=0\n", encoding="utf-8")
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(worktree),
            "launch": "in-harness",
            "role": "implement",
            "backend": "native",
            "attempt": 1,
            "phase": "delivered",
            "created_at": "2026-11-20T07:44:37Z",
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "manifest_path": str(manifest),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": SECTION,
                "time_budget": "35m",
                "write_paths": ["reviewed.txt"],
            },
        },
    )
    return {
        "origin": origin,
        "repository": repository,
        "worktree": str(worktree),
        "run_id": RUN_ID,
        "head": head,
        "gate_log": gate_log,
    }


def _edit_and_commit_after_the_snapshot(
    monkeypatch: pytest.MonkeyPatch, edit: Callable[[Any], None]
) -> None:
    """Run a peer's plan edit between the snapshot and the landing's write.

    ``_record_comment_with_snapshot`` runs after the snapshot
    :func:`promotion._preserve_plan_on_refusal` takes and before the comment is
    written, which is the window a coordinator's own plan edit landed in.
    """
    real = promotion._record_comment_with_snapshot

    def wrapper(snapshot, **kwargs):
        if snapshot is not None:
            edit(snapshot)
        return real(snapshot, **kwargs)

    monkeypatch.setattr(promotion, "_record_comment_with_snapshot", wrapper)


def _commit_a_plan_edit(repository: Path) -> None:
    path = repository / PLAN_PATH
    text = path.read_text(encoding="utf-8")
    state = _plan_html.read_state(text)
    state["summary"] = "The coordinator edited this plan after the snapshot"
    state["version"] = int(state.get("version") or 0) + 1
    path.write_text(_plan_html.write_state(text, state), encoding="utf-8")
    _commit_plan(repository, "docs: the coordinator's own plan edit")


def _commit_a_section_removal(repository: Path) -> None:
    _write_plan(
        repository,
        _plan_document(paragraph="The authored text.", with_section=False),
    )
    _commit_plan(repository, "docs: the section moves out of this plan")


def test_a_plan_edited_between_the_snapshot_and_the_landing_still_lands(
    run: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = run["repository"]
    _edit_and_commit_after_the_snapshot(
        monkeypatch, lambda _snapshot: _commit_a_plan_edit(repository)
    )

    result = _complete_land(run)

    assert result.exit_code == 0, result.output
    committed = _git(repository, "show", f"HEAD:{PLAN_PATH}")
    assert "promoted" in committed, committed
    assert "The coordinator edited this plan after the snapshot" in committed, committed
    assert "The authored text." in committed, committed
    assert _git(repository, "status", "--porcelain", "--", PLAN_PATH) == "", (
        "the landing left the plan uncommitted"
    )
    assert _is_ancestor(run["origin"], run["head"], "main"), (
        "the reviewed head never landed"
    )
    assert _git(run["origin"], "show", "main:reviewed.txt") == MARKER.strip()


def test_a_section_removed_between_the_snapshot_and_the_landing_refuses(
    run: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = run["repository"]
    _edit_and_commit_after_the_snapshot(
        monkeypatch, lambda _snapshot: _commit_a_section_removal(repository)
    )

    result = _complete_land(run)

    assert result.exit_code != 0, result.output
    assert SECTION in result.output, result.output
    assert not _is_ancestor(run["origin"], run["head"], "main"), (
        "a refused landing merged the reviewed head"
    )
    assert "promoted" not in _git(repository, "show", f"HEAD:{PLAN_PATH}")
    row = ledger.run_path(PROJECT, run["run_id"], repository)
    assert not Path(row).exists(), f"a refused landing wrote a ledger row at {row}"
