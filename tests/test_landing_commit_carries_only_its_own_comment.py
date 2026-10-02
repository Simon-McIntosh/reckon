"""A landing commit carries the run's own plan comment and nothing else.

An idempotent re-promotion carries the plan file whenever it differs from
HEAD, because an earlier attempt that recorded the comment and then failed to
commit it leaves the comment on disk. That rule carried the *whole* file, so
an unrelated uncommitted edit to the plan was swept into the landing commit.

Promotion now rebuilds the expected working copy from the plan at HEAD: its
parsed state takes up this run's one comment and the two version stamps the
versioned write always moves, rendered back through the store's own writer and
read as parsed HTML. The file is carried only when the difference from HEAD is
a write the store made — an appended landing comment, a moved stamp or the
store's own re-encoding; an unrelated authored edit refuses before either
store is written, so a refused promotion leaves neither a ledger row nor a
comment for the next promotion to read as an unrelated edit.

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
RUN_ID_SECOND = "r-20260917T140000000002-carries-only-its-own-comment"
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


def _pointer(repository: Path, run_id: str = RUN_ID) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
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


def _promote(repository: Path, narrative: str, run_id: str = RUN_ID) -> dict[str, Any]:
    return crew.complete(run_id, gate="passed", outcome=narrative, root=repository)


def _dirty(repository: Path, relative: Path | str) -> str:
    return _git(repository, "status", "--porcelain", "--", str(relative))


def _promotion_commit_files(repository: Path, run_id: str = RUN_ID) -> list[str]:
    for line in _git(repository, "log", "--format=%H%x09%s").splitlines():
        sha, _, subject = line.partition("\t")
        if subject.startswith(f"promote({run_id})"):
            shown = _git(
                repository, "show", "--no-walk", "--name-only", "--format=", sha
            )
            return [path for path in shown.splitlines() if path]
    raise AssertionError(f"no landing commit for {run_id}")


def _fail_first_landing(repository: Path, narrative: str) -> None:
    """Hold index.lock so the first landing's commit fails, comment uncommitted."""
    lock = repository / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    try:
        with pytest.raises(CrewError):
            _promote(repository, narrative)
    finally:
        lock.unlink()


def _plan_comments(repository: Path) -> list[str]:
    state, _version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    return [item["id"] for item in state.get("comments", {}).get("s2", [])]


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


def test_the_refusal_message_names_the_admitted_plan_state_writes(
    repository: Path,
) -> None:
    """The guard admits a plan-state write the store made, so its refusal must
    name that admitted class and say the refused difference is authored content
    outside the store-owned regions — not merely list the two writes it once
    knew about."""
    narrative = "a landing refused for an unrelated edit, to read its message"
    _pointer(repository)
    _fail_first_landing(repository, narrative)

    plan_path = repository / PLAN_RELATIVE
    text = plan_path.read_text(encoding="utf-8")
    plan_path.write_text(
        text.replace("</main>", f"<p>{UNRELATED}</p></main>"), encoding="utf-8"
    )

    with pytest.raises(CrewError) as caught:
        _promote(repository, narrative)

    message = str(caught.value)
    assert "plan-state write the store made" in message
    assert "authored content outside those store-owned regions" in message


# ── The store's own plan-state write is the run's bookkeeping, not a refusal ─


def test_a_store_written_impl_move_lands_as_the_runs_own_bookkeeping(
    repository: Path,
) -> None:
    """A plan-state change the plan store made — here an impl move — is the
    run's own bookkeeping, so the landing admits it and carries it into the
    landing commit rather than refusing it as an unrelated edit."""
    narrative = "a landing whose plan differs by a store-written impl move"
    _pointer(repository)

    # Move the impl through the plan store, exactly as a plan-state write does,
    # and commit nothing more.
    state, version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    state["impl"] = 0.75
    _store.write_plan(PROJECT, PLAN, state, version, repository, artifact_type="plan")

    plan_path = repository / PLAN_RELATIVE
    assert _dirty(repository, plan_path), "the store write should be dirty"

    result = _promote(repository, narrative)

    assert result["plan_comment"]["recorded"] is True
    assert _dirty(repository, plan_path) == ""
    assert PLAN_RELATIVE in _promotion_commit_files(repository)
    assert 'name="plan-impl" content="0.75"' in _git(
        repository, "show", f"HEAD:{PLAN_RELATIVE}"
    )


# ── The cascade: a refusal strands nothing for the next promotion ────────────


def test_a_refused_landing_strands_nothing_for_the_next_promotion(
    repository: Path,
) -> None:
    """Two consecutive promotions on one plan: the first is refused for an
    unrelated edit and leaves nothing behind; the second lands once that edit
    is committed."""
    first = "a first landing refused for an unrelated edit"
    second = "a second landing that lands once the edit is committed"
    _pointer(repository, RUN_ID)

    plan_path = repository / PLAN_RELATIVE
    text = plan_path.read_text(encoding="utf-8")
    plan_path.write_text(
        text.replace("</main>", f"<p>{UNRELATED}</p></main>"), encoding="utf-8"
    )
    head_before = _git(repository, "rev-parse", "HEAD")

    with pytest.raises(CrewError):
        _promote(repository, first, RUN_ID)

    # The refusal runs before the comment is written, so nothing is stranded:
    # HEAD is untouched and no landing comment reached the plan.
    assert _git(repository, "rev-parse", "HEAD") == head_before
    assert _plan_comments(repository) == []

    # Commit the unrelated edit; the same promotion now lands.
    _git(repository, "add", PLAN_RELATIVE)
    _git(repository, "commit", "-q", "-m", "test: commit the unrelated edit")

    _pointer(repository, RUN_ID_SECOND)
    result = _promote(repository, second, RUN_ID_SECOND)

    assert result["plan_comment"]["recorded"] is True
    assert _dirty(repository, plan_path) == ""
    assert _promotion_commit_files(repository, RUN_ID_SECOND)


# ── Tolerance: the store's own re-encoding is not an unrelated edit ──────────


def test_a_plan_stored_before_the_writers_canonical_encoding_lands(
    repository: Path,
) -> None:
    """A plan whose stored HTML is not the writer's canonical encoding still
    lands: the store decodes an entity on read and re-emits it canonically, so
    an encoding-only difference must not read as an unrelated authored edit."""
    narrative = "a landing on a plan stored before the writer's canonical encoding"
    plan_path = repository / PLAN_RELATIVE

    # Seed a comment whose stored form is not what the writer emits: the store
    # decodes ``&#x27;`` on read and re-emits a plain apostrophe.
    seeded = plan_path.read_text(encoding="utf-8").replace(
        "</main>",
        '<div class="r-comment" data-section="s2" data-id="c-run-earlier"'
        ' data-who="reckon-build" data-when="2026-09-17T10:00:00Z">'
        '<div class="r-comment-body"><p>it&#x27;s earlier work</p></div></div></main>',
    )
    plan_path.write_text(seeded, encoding="utf-8")
    _git(repository, "add", PLAN_RELATIVE)
    _git(repository, "commit", "-q", "-m", "test: seed a non-canonical comment")

    # A store write would canonicalise that entity. Leave the re-encoded copy
    # uncommitted, as a landing whose commit failed would.
    plan_path.write_text(seeded.replace("&#x27;", "'"), encoding="utf-8")
    assert _dirty(repository, plan_path), "the encoding-only difference must be dirty"

    _pointer(repository)
    result = _promote(repository, narrative)

    assert result["plan_comment"]["recorded"] is True
    assert _dirty(repository, plan_path) == ""
    assert PLAN_RELATIVE in _promotion_commit_files(repository)
