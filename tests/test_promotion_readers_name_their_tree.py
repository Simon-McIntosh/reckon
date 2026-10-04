"""Every promotion reader resolves the run's tree from the run's own record.

Five readers in ``reckon/crew/promotion.py`` once built their tree as
``Path(str(record.get("worktree") or ""))``, and ``Path("")`` is ``Path(".")``:
a record whose worktree field was blank therefore resolved its tree to whatever
repository the promotion happened to start in. For the two revision readers —
``_require_commits_beyond_base`` and ``_review_changed_scope`` — that is a
confident wrong answer about the run's work: a citation is measured against a
repository the run was never dispatched for.

The two landing readers, ``_landing_preconditions`` and ``_complete_locked``,
measure the run's citations, scope and shadow patch in the same tree, so the
fallback was replaced there too rather than kept as a caller default: the tree
a promotion lands into is the record's ``repo``, resolved separately as the
checkout, and the cwd is never that tree. A record that names no readable
worktree or repository now refuses in the landing preconditions, and
``_complete_locked`` reads the absence as no measurement rather than consulting
an ambient checkout.

Each test runs the reader from inside an unrelated git repository; the
unrelated repository's own plan and commit are made adversarial, so a reader
that fell back to the cwd would answer with that repository's facts.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon import _plan_html, _store, crew, ledger
from reckon.crew import promotion
from reckon.crew.node import CrewError
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "readers-name-their-tree-fixture"
PLAN = "promotion-readers-target"
RUN_IDS = (
    "r-20261004T120000000001-ambient-revision-reader",
    "r-20261004T120000000002-ambient-landing-reader",
    "r-20261004T120000000003-named-tree",
)


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _seed_repository(root: Path, marker: str) -> str:
    """Two commits whose content differs per repository, so heads never collide.

    The measured commit is the second one: a root commit has no first parent,
    so there is no diff for a scope reader to measure.
    """
    root.mkdir(parents=True)
    (root / "file.txt").write_text(f"seed {marker}\n", encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    _git(root, "add", "file.txt")
    _git(root, "commit", "-q", "-m", f"seed {marker}")
    (root / "file.txt").write_text(f"seed {marker}\nwork {marker}\n", encoding="utf-8")
    _git(root, "add", "file.txt")
    _git(root, "commit", "-q", "-m", f"work {marker}")
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture()
def unrelated_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, str]:
    """A git repository the run was never dispatched for, as the cwd."""
    root = tmp_path / "unrelated"
    head = _seed_repository(root, "unrelated")
    monkeypatch.chdir(root)
    return root, head


# ── The two revision readers ────────────────────────────────────────────────


def test_require_commits_beyond_base_ignores_the_ambient_repository(
    unrelated_repository: tuple[Path, str],
) -> None:
    """A blank field names no tree, so the ambient repository's base is not read.

    The unrelated repository's head is both the recorded base and the citation,
    which is the shape the guard exists to refuse — but only in the run's own
    tree. A reader that fell back to the cwd would raise here.
    """
    _root, ambient_head = unrelated_repository
    record = {"base_sha": ambient_head, "worktree": "", "repo": ""}

    promotion._require_commits_beyond_base(RUN_IDS[0], record, [ambient_head])


def test_require_commits_beyond_base_still_refuses_for_a_named_tree(
    unrelated_repository: tuple[Path, str],
) -> None:
    """The positive control: the same facts refuse once the tree is named."""
    root, ambient_head = unrelated_repository
    record = {"base_sha": ambient_head, "worktree": "", "repo": str(root)}

    with pytest.raises(CrewError, match="is the base it was dispatched against"):
        promotion._require_commits_beyond_base(RUN_IDS[0], record, [ambient_head])


def test_review_changed_scope_ignores_the_ambient_repository(
    unrelated_repository: tuple[Path, str],
) -> None:
    """A blank field measures no commit and grants the fuller review."""
    _root, ambient_head = unrelated_repository

    paths, lines, measured = promotion._review_changed_scope(
        RUN_IDS[0], {"worktree": "", "repo": ""}, [ambient_head]
    )

    assert (paths, lines, measured) == ((), None, False)


def test_review_changed_scope_measures_only_the_named_tree(
    tmp_path: Path, unrelated_repository: tuple[Path, str]
) -> None:
    """The positive control: a named tree still measures its own citation."""
    _root, ambient_head = unrelated_repository
    run_tree = tmp_path / "run-tree"
    run_head = _seed_repository(run_tree, "run")

    paths, lines, measured = promotion._review_changed_scope(
        RUN_IDS[2], {"worktree": "", "repo": str(run_tree)}, [run_head]
    )

    assert measured is True
    assert paths == ("file.txt",)
    assert lines == 1
    assert ambient_head not in paths


# ── The two landing readers ─────────────────────────────────────────────────


def test_landing_preconditions_refuses_when_no_tree_is_named(
    unrelated_repository: tuple[Path, str], tmp_path: Path
) -> None:
    """A citation with no readable tree refuses rather than reading the cwd.

    The refusal names the absence, and nothing resolves the citation against
    the unrelated repository the test runs in.
    """
    _root, ambient_head = unrelated_repository
    record = {
        "project": PROJECT,
        "worktree": "",
        "repo": "",
        "role": "implement",
        "node": {"plan": PLAN, "section": "s2"},
    }

    with pytest.raises(CrewError) as error:
        promotion._landing_preconditions(
            RUN_IDS[0],
            record,
            checkout=None,
            ledger_root=tmp_path / "ledger",
            commits=(ambient_head,),
            gate="passed",
            failure_classification="",
            no_impl_change="",
            plan_link="",
            unplanned_reason="",
            boundary_waiver="",
            negative_control_waiver=None,
            accepted_paths=None,
            gate_check=None,
            require_gate_check=False,
        )

    assert "names no readable worktree or repository" in str(error.value)


@pytest.fixture()
def promotion_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fixture repository and crew home the promotion may write into."""
    config_hook = tmp_path / "config"
    config_hook.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_hook))
    root = tmp_path / "repo"
    plan_file = root / "docs" / "plans" / f"{PLAN}.html"
    plan_file.parent.mkdir(parents=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    state = {
        "type": "plan",
        "slug": PLAN,
        "title": "Promotion readers target",
        "status": "active",
        "version": 0,
        "comments": {},
    }
    plan_file.write_text(_plan_html.write_state(bare, state), encoding="utf-8")
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    _git(root, "add", "docs")
    _git(root, "commit", "-q", "-m", "seed the promotion target")
    (config_hook / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _ambient_plan_repository(tmp_path: Path, run_id: str) -> tuple[Path, str]:
    """An unrelated repository whose plan already holds this run's comment id."""
    ambient = tmp_path / "ambient"
    plan_dir = ambient / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    (plan_dir / f"{PLAN}.html").write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f'</head><body><p data-id="c-run-{run_id}"></p></body></html>\n',
        encoding="utf-8",
    )
    _git(ambient, "init", "-q", "-b", "main")
    _git(ambient, "config", "user.email", "worker@example.invalid")
    _git(ambient, "config", "user.name", "Worker")
    _git(ambient, "add", "docs")
    _git(ambient, "commit", "-q", "-m", "a plan this run never wrote")
    return ambient, _git(ambient, "rev-parse", "HEAD")


def test_complete_locked_lands_the_comment_the_record_tree_would_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    promotion_target: Path,
) -> None:
    """A record naming no tree does not read the ambient checkout's plan.

    The run's manifest declares a commit, and the ambient repository holds a
    plan at that commit already carrying this run's comment id, so a reader
    that consulted the cwd would read the record as worker-authored and skip
    the landing comment. The record names no tree, so the cwd is not consulted
    and the comment is recorded.
    """
    run_id = RUN_IDS[1]
    ambient, ambient_head = _ambient_plan_repository(tmp_path, run_id)
    monkeypatch.chdir(ambient)
    manifest = tmp_path / "manifest.md"
    manifest.write_text(
        f"status: complete\ncommits: [{ambient_head}]\n", encoding="utf-8"
    )
    record: dict[str, Any] = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": "",
        "worktree": "",
        "base_sha": "",
        "role": "implement",
        "launch": "in-harness",
        "member": "worker-a",
        "backend": "native",
        "created_at": "2026-10-04T11:00:00Z",
        "manifest_path": str(manifest),
        "node": {"id": PLAN, "plan": PLAN, "section": "s2", "write_paths": []},
    }
    _write_json(pointer_path(run_id), record)
    landing = {
        "already_landed": False,
        "commits": [],
        "shadow_patch": "",
        "changed_lines": None,
        "scope_acceptances": [],
        "boundary_waived": None,
        "brief_owner": None,
        "impl_move": {},
        "manifest": None,
        "manifest_text": None,
        "negative_control": {"verdict": "waived", "reason": "fixture"},
    }

    result = crew._complete_locked(
        run_id,
        record=record,
        gate="passed",
        outcome="the landing the record's own tree owns",
        root=promotion_target,
        landing=landing,
    )

    assert result["plan_comment"]["recorded"] is True
    state, _version = _store.read_plan(
        PROJECT, PLAN, promotion_target, artifact_type="plan"
    )
    assert state["comments"]["s2"][0]["id"] == f"c-run-{run_id}"
    # The ambient repository's plan is untouched: it belongs to no run here.
    assert f"c-run-{run_id}" in (ambient / "docs" / "plans" / f"{PLAN}.html").read_text(
        encoding="utf-8"
    )
    rows = ledger.runs(PROJECT, root=promotion_target)
    assert [row["run_id"] for row in rows] == [run_id]
