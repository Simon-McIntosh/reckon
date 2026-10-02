"""A landing commit carries the run's own plan comment and nothing else.

An idempotent re-promotion carries the plan file whenever it differs from
HEAD, because an earlier attempt that recorded the comment and then failed to
commit it leaves the comment on disk. That rule carried the *whole* file, so
an unrelated uncommitted edit to the plan — authored prose, a stale scalar —
was swept into the landing commit and attributed to the run.

Promotion now rebuilds the expected working copy from the plan at HEAD: its
parsed state takes up this run's one comment and the two version stamps the
versioned write always moves, rendered back with the HEAD text's authored
prose as the base. The file is carried only when the working copy matches that
byte for byte; any other change refuses before the commit, names the plan
file, and leaves the edit where it was in the working tree.

These tests synthesise a fixture repository and crew home per case and assert
the real plan and crew directories are untouched, because an isolated read
does not prove an isolated write.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon import _plan_html, _store, crew
from reckon.crew.node import CrewError
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "landing-comment-fixture"
PLAN = "landing-comment-typo-target"
RUN_ID = "r-20260917T140000000001-carries-only-its-own-comment"
PLAN_RELATIVE = f"docs/plans/{PLAN}.html"
UNRELATED = "an unrelated authored edit to the plan"


def _git(repository: Path, *arguments: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode:
        raise AssertionError(
            f"git {' '.join(arguments)} failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _write_plan(root: Path) -> Path:
    path = root / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    state = {
        "type": "plan",
        "slug": PLAN,
        "title": "Landing comment target",
        "status": "active",
        "version": 0,
        "comments": {},
    }
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def real_stores_are_not_fixture_targets() -> None:
    """No fixture may reach this checkout's own plan."""
    checkout = Path(__file__).resolve().parents[1]
    assert not (checkout / PLAN_RELATIVE).exists()
    assert not (checkout / "docs" / "state" / PROJECT).exists()
    yield
    assert not (checkout / PLAN_RELATIVE).exists()
    assert not (checkout / "docs" / "state" / PROJECT).exists()


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_hook = tmp_path / "config"
    config_hook.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_hook))
    root = tmp_path / "repo"
    _write_plan(root)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
        ("commit", "-q", "-m", "test: seed repository"),
    ):
        _git(root, *arguments)
    (config_hook / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _pointer(repository: Path) -> None:
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-09-17T11:00:00Z",
            "manifest_path": "/durable/manifest.json",
            "node": {
                "id": "landing-comment-carries-only-its-own-comment",
                "plan": PLAN,
                "section": "§2",
                "time_budget": "25m",
                "write_paths": [PLAN_RELATIVE],
            },
        },
    )


def _promote(repository: Path, narrative: str) -> dict[str, Any]:
    return crew.complete(RUN_ID, gate="passed", outcome=narrative, root=repository)


def _dirty(repository: Path, relative: Path | str) -> str:
    return _git(repository, "status", "--porcelain", "--", str(relative))


def _promotion_commit_files(repository: Path) -> list[str]:
    for line in _git(repository, "log", "--format=%H%x09%s").splitlines():
        sha, _, subject = line.partition("\t")
        if subject.startswith(f"promote({RUN_ID})"):
            shown = _git(
                repository, "show", "--no-walk", "--name-only", "--format=", sha
            )
            return [path for path in shown.splitlines() if path]
    raise AssertionError(f"no landing commit for {RUN_ID}")


def _fail_first_landing(repository: Path, narrative: str) -> None:
    """Hold index.lock so the first landing's commit fails, comment uncommitted."""
    lock = repository / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    try:
        with pytest.raises(CrewError):
            _promote(repository, narrative)
    finally:
        lock.unlink()


# ── The named case: only the run's own comment, so it is carried ─────────────


def test_a_re_promotion_commits_a_plan_differing_only_by_its_own_comment(
    repository: Path,
) -> None:
    narrative = "a landing whose plan difference is only its own comment"
    _pointer(repository)
    _fail_first_landing(repository, narrative)

    plan_path = repository / PLAN_RELATIVE
    assert _dirty(repository, plan_path), "the plan file should be dirty"

    result = _promote(repository, narrative)

    assert result["plan_comment"]["already_recorded"] is True
    assert _dirty(repository, plan_path) == ""
    assert PLAN_RELATIVE in _promotion_commit_files(repository)

    state, _version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    bodies = [item["body"] for item in state["comments"]["s2"]]
    assert sum(narrative in body for body in bodies) == 1


# ── The refusal: an unrelated edit must not be swept in ──────────────────────


def test_an_unrelated_edit_to_the_plan_refuses_before_committing(
    repository: Path,
) -> None:
    narrative = "a landing that must refuse to sweep an unrelated edit"
    _pointer(repository)
    _fail_first_landing(repository, narrative)

    plan_path = repository / PLAN_RELATIVE
    text = plan_path.read_text(encoding="utf-8")
    assert UNRELATED not in text
    plan_path.write_text(
        text.replace("</main>", f"<p>{UNRELATED}</p></main>"), encoding="utf-8"
    )
    head_before = _git(repository, "rev-parse", "HEAD")

    with pytest.raises(CrewError) as caught:
        _promote(repository, narrative)

    # The plan file is named, and the unrelated edit is left where it was.
    assert str(plan_path) in str(caught.value)
    assert _dirty(repository, plan_path), "the unrelated edit must remain uncommitted"
    assert UNRELATED in plan_path.read_text(encoding="utf-8")
    assert _git(repository, "rev-parse", "HEAD") == head_before
