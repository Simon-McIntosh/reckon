"""A batch derives its project once, whichever checkout it runs from.

A dispatch from a worktree names two docs directories for one project: queue
ordering resolves the project's registered mount, while a section's wait check
names the checkout's own. Keying the batch's one-derivation cache on the
resolved directory therefore bought a whole-project scan for each of them, so
the same batch cost two derivations from a worktree and one from the main
checkout.

This counts the derivations a batch of three entries costs when it runs from a
worktree whose docs tree is a different directory from the mount. The count is
one; the base revision, entries derived under the checkout's own key, is two.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon.crew import dispatch_batch, runs

PROJECT = "proj"
SESSION = "coordinator-worktree-batch"
PLANS = ("plan-a", "plan-b", "plan-c")
SECTION = "s1"


def _config() -> dict:
    return {
        "default_backend": "clive",
        "local_backend": "clive",
        "backends": {
            "clive": {
                "launch": "cli",
                "command": "codex",
                "model": "local-model",
                "effort": "medium",
                "sandbox": "worktree-full",
                "time_budget": "25m",
            }
        },
        "roles": {"implement": {}},
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }


def _plan(slug: str) -> str:
    quote = '"'
    declarations = json.dumps({SECTION: "implementable"})
    body = (
        f"<h2 id={quote}{SECTION}{quote}>§1</h2>"
        f"<p>Build {slug} {SECTION}.</p>"
        f"<p><strong>Done when</strong> the {slug} check passes.</p>"
    )
    return (
        "<!doctype html>\n"
        f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>\n"
        f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>\n"
        f"<meta name={quote}plan-slug{quote} content={quote}{slug}{quote}>\n"
        f"<meta name={quote}plan-status{quote} content={quote}active{quote}>\n"
        f"<meta name={quote}plan-section-declarations{quote} content={quote}"
        f"{declarations.replace(quote, '&quot;')}{quote}>\n"
        f"{body}\n"
    )


def _entries() -> list[dict]:
    return [
        {
            "project": PROJECT,
            "plan": slug,
            "section": SECTION,
            "node": f"{slug}-{SECTION}",
            "goal": f"build {slug} {SECTION}",
            "time_budget": "20m",
            "spec_level": "exact",
        }
        for slug in PLANS
    ]


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """A main checkout with three authored plans, and a worktree of it."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    main = tmp_path / "checkout"
    state = main / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    plans_dir = main / "docs" / "plans"
    for slug in PLANS:
        plans_dir.mkdir(parents=True, exist_ok=True)
        (plans_dir / f"{slug}.html").write_text(_plan(slug), encoding="utf-8")
    _git(main, "init", "-q", "-b", "main")
    _git(main, "config", "user.email", "worker@example.invalid")
    _git(main, "config", "user.name", "Worker")
    _git(main, "add", "--", "docs")
    _git(main, "commit", "-q", "-m", "chore: seed checkout")

    worktree = tmp_path / "worktree"
    _git(main, "worktree", "add", "-q", "-b", "discover-once", str(worktree))
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(main / "docs")}), encoding="utf-8"
    )
    return main, worktree


def _queue(repository: Path) -> dict:
    def config_for(_project: str) -> dict:
        return json.loads(json.dumps(_config()))

    return dispatch_batch.queue_batch(
        _entries(),
        session=SESSION,
        config_for=config_for,
        repo_root=repository,
    )


def test_a_batch_from_a_worktree_derives_the_project_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import reckon.mcp_discovery as discovery

    main, worktree = _checkout(tmp_path, monkeypatch)

    derived: list[str] = []
    original = discovery._discover_project

    def counted(project: str, root: str | None = None):
        derived.append(str(root or ""))
        return original(project, root)

    monkeypatch.setattr(discovery, "_discover_project", counted)

    result = _queue(worktree)

    assert result["refused"] == 0, result["results"]
    assert result["queued"] == len(PLANS)
    assert len(derived) == 1, (
        "three entries over one project, dispatched from a worktree whose docs "
        f"tree is not the registered mount, derive the project once: {derived}"
    )
    assert sorted(
        str((pointer.get("node") or {}).get("id") or "")
        for pointer in runs.list_live(project=PROJECT)
    ) == sorted(f"{slug}-{SECTION}" for slug in PLANS)