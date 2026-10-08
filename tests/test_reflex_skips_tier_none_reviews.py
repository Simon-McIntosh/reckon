"""The review reflex dispatches no review for a run whose tier is none.

A run that changes no runtime source — documents, evidence, research data,
figures — lands with the review gate standing down, and promotion records its
tier as ``none`` so the absence of a review is a stated reason rather than a
silent gap. The reflex reads the same tier and must skip such a run: composing
a review for it spends a member and a lane on a diff no reviewer is owed, and
each skipped run is one fewer dispatch competing for the fleet.

The tier is resolved through the very resolver promotion calls, so the test
drives the sweep with the dispatch stubbed out. A stubbed dispatch makes the
claim directly: whether a review was composed is visible in the recorded call,
whereas a review that ran and produced nothing is not visible in any output.
The two runs are swept together, because a guard that only ever quietens is
indistinguishable from one that has been deleted — the runtime-source run must
still be dispatched in the same sweep that skips the documentation one.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, crew
from reckon.crew import recovery, recovery_review_acceptance, recovery_watch, runs

PROJECT = "sample"
PLAN = "fixture"
SESSION = "session-orchestrating"

# More changed source lines than the shipped light ceiling, so the runtime-source
# run resolves to ``full`` rather than ``light`` and the sweep still dispatches.
_LARGE_SOURCE = "".join(f"VALUE_{index} = {index}\n" for index in range(80))

CONFIG = {
    "default_backend": "alpha",
    "local_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=root, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _write_plan(root: Path) -> None:
    plan = root / "docs" / "plans" / f"{PLAN}.html"
    plan.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc">'
        '<h2 id="s2">Section two</h2>'
        "</main></body></html>\n"
    )
    state: dict = {
        "type": "plan",
        "slug": PLAN,
        "title": "Fixture",
        "status": "active",
        "version": 0,
        "comments": {},
    }
    plan.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_plan(root)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return config_home, root


def _commit_change(root: Path, relative_path: str, content: str) -> str:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(root, "add", relative_path)
    _git(root, "commit", "-q", "-m", "feature: change a path")
    return _git(root, "rev-parse", "HEAD")


def _write_pointer(
    config_home: Path,
    root: Path,
    run_id: str,
    *,
    commit: str,
    changed_paths: tuple[str, ...],
    spec_level: str = "exact",
) -> None:
    manifest = config_home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: node-a\n"
        "status: complete\n"
        f"commits: {commit}\n"
        f"changed_paths: {', '.join(changed_paths)}\n"
        "tests: focused check passed\n",
        encoding="utf-8",
    )
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(root),
            "worktree": str(root),
            "launch": "cli",
            "role": "implement",
            "backend": "alpha",
            "argv": ["codex"],
            "phase": "starting",
            "process_alive": False,
            "session": SESSION,
            "manifest_path": str(manifest),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "s2",
                "spec_level": spec_level,
                "write_paths": list(changed_paths),
            },
        },
    )


def test_the_sweep_skips_a_tier_none_run_and_dispatches_a_source_run(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home, root = project
    docs_commit = _commit_change(
        root, "docs/evidence/fixture-note.html", "<p>a note</p>\n"
    )
    source_commit = _commit_change(root, "reckon/thing.py", _LARGE_SOURCE)
    _write_pointer(
        config_home,
        root,
        "r-docs-only",
        commit=docs_commit,
        changed_paths=("docs/evidence/fixture-note.html",),
    )
    _write_pointer(
        config_home,
        root,
        "r-source",
        commit=source_commit,
        changed_paths=("reckon/thing.py",),
    )

    composed: list[str] = []

    def stub_dispatch(record, **_kwargs):
        run_id = str(record.get("run_id") or "")
        composed.append(run_id)
        return {
            "run_id": run_id,
            "dispatched": True,
            "review_run_id": f"r-review-{run_id}",
            "reason": "stub: the composed review was launched",
        }

    (monkeypatch.setattr(recovery_review_acceptance, "dispatch_review_for_run", stub_dispatch), monkeypatch.setattr(recovery_watch, "dispatch_review_for_run", stub_dispatch))

    result = recovery.dispatch_awaiting_reviews(
        project=PROJECT,
        config=CONFIG,
        launcher=lambda *args, **kwargs: 1,
        session=SESSION,
    )

    # The runtime-source run is still dispatched in the same sweep: the skip is
    # keyed on the tier and not on something else the sweep changed for every
    # run, which is what a guard that only ever quietens would look like.
    assert composed == ["r-source"], composed
    assert result["dispatched"] == ["r-review-r-source"]

    skip_reports = [
        report for report in result["reports"] if report.get("review_tier") == "none"
    ]
    assert [report["run_id"] for report in skip_reports] == ["r-docs-only"]
    assert skip_reports[0]["dispatched"] is False
    assert "runtime source" in skip_reports[0]["reason"]

    recorded = runs.read_pointer("r-docs-only")["review_dispatch"]
    assert recorded["status"] == "skipped"
    assert "runtime source" in recorded["reason"]
    source_recorded = runs.read_pointer("r-source").get("review_dispatch") or {}
    assert source_recorded.get("status") != "skipped"
