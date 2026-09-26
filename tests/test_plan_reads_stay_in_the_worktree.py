"""A worker's plan reads come from its own worktree.

A crew worker runs with ``RECKON_RUN_ID`` in its environment and its own git
worktree recorded on the live run pointer. Its MCP server inherits both. A plan
read that names no ``checkout_path`` must therefore resolve to the worker's own
worktree for the run's own project — the revision the worker holds — rather
than the mounts-registered main checkout, whose copy is the coordinator's live
state. Reading the coordinator's version is how a worker came to write against
a version its own tree never had.

The same run scope reaches a plan write that would leave the worktree. The
registered write entry point redirects a run's own project into the worktree;
the granular mutators take no ``checkout_path`` and cannot, so a run-scoped call
to one is refused, naming the run and the path it would have written, rather
than returning ``ok``.

Two of the fixtures here matter for what they can *hide* rather than what they
show. One worktree copy carries a plan the main checkout does not have, so a
read that resolved main would report a smaller inventory — the count itself is
the discriminator, not a difference in one file's content. The other fixture
leaves the worktree plan byte-identical to main's, which is the case a
resolution bug survives in: a copy that has diverged shows up whichever file
was read, and only an unmodified pair proves the *path* was scoped.

These tests build a synthetic repository and a synthetic run pointer; they
never touch the real ones. The main checkout's plan file is asserted
byte-identical after a run-scoped call that must not reach it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import reckon.crew.runs as crew_runs
from reckon import mcp as mcp_module
from reckon._store import new_plan_html, read_plan, write_plan

PROJECT = "proj"
OTHER_PROJECT = "other"
SLUG = "sample-plan"
#: A plan written into the run's worktree only. A read that resolves the main
#: checkout cannot see it, so inventory counts discriminate the two trees.
WORKTREE_ONLY_SLUG = "worktree-only-plan"
RUN_ID = "r-20260101T000000000000-sample-node"
NODE = "sample-node"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_plan(root: Path, project: str, slug: str) -> Path:
    """Write one minimal plan into ``<root>/docs/plans/<slug>.html``."""
    plans_dir = root / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    path = plans_dir / f"{slug}.html"
    path.write_text(new_plan_html(project, slug), encoding="utf-8")
    return path


def _build(tmp_path, monkeypatch, *, divergent: bool) -> dict[str, object]:
    """A main checkout, a run worktree, a mounted sibling and a live pointer.

    ``divergent`` writes the worktree's copy of the shared plan one version
    ahead of main's and adds a plan the main checkout does not have, so a read
    says which tree it came from. ``divergent=False`` leaves the two copies
    byte-identical, which is the case where only the resolved path can tell
    them apart.
    """
    main = tmp_path / "main"
    worktree = tmp_path / "worktree"
    other_repo = tmp_path / "other_repo"
    main_plan = _write_plan(main, PROJECT, SLUG)
    worktree_plan = _write_plan(worktree, PROJECT, SLUG)
    other_main_plan = _write_plan(other_repo, OTHER_PROJECT, SLUG)

    extra_plan: Path | None = None
    if divergent:
        worktree_data, version = read_plan(PROJECT, SLUG, str(worktree))
        worktree_data["title"] = "worktree copy"
        write_plan(PROJECT, SLUG, worktree_data, version, root=str(worktree))
        extra_plan = _write_plan(worktree, PROJECT, WORKTREE_ONLY_SLUG)

    mounts = tmp_path / "mounts.json"
    mounts.write_text(
        json.dumps(
            {
                PROJECT: str(main / "docs"),
                OTHER_PROJECT: str(other_repo / "docs"),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(tmp_path / "state"))

    live = tmp_path / "live"
    live.mkdir()
    pointer = {
        "run_id": RUN_ID,
        "node": {"id": NODE},
        "project": PROJECT,
        "worktree": str(worktree),
        "repo": str(main),
    }
    (live / f"{RUN_ID}.json").write_text(json.dumps(pointer), encoding="utf-8")
    monkeypatch.setattr(crew_runs, "live_dir", lambda: live)
    monkeypatch.setenv("RECKON_RUN_ID", RUN_ID)

    return {
        "main": main,
        "worktree": worktree,
        "other_repo": other_repo,
        "main_plan": main_plan,
        "worktree_plan": worktree_plan,
        "other_plan": other_main_plan,
        "extra_plan": extra_plan,
    }


@pytest.fixture()
def run_scoped(tmp_path, monkeypatch):
    """A run whose worktree copy differs from the main checkout's."""
    return _build(tmp_path, monkeypatch, divergent=True)


@pytest.fixture()
def run_scoped_identical(tmp_path, monkeypatch):
    """A run whose worktree copy is byte-identical to the main checkout's."""
    return _build(tmp_path, monkeypatch, divergent=False)


def _read(*, project, slug=SLUG, checkout_path=None):
    return mcp_module._read_plan(
        project=project,
        slug=slug,
        checkout_path=checkout_path,
    )


def _version(root: Path, project: str, slug: str) -> int:
    _data, version = read_plan(project, slug, str(root))
    return version


def _resource_read(slug: str, *, checkout_path: str | None = None):
    """Drive the typed-resource branch of ``read_plan`` for one plan."""
    return mcp_module._read_plan_view(
        project=None,
        slug=None,
        checkout_path=checkout_path,
        doc_type=None,
        resource={"project": PROJECT, "type": "plan", "id": slug},
        view="version",
        section=None,
        cursor=None,
        limit=None,
        include_prompts=False,
        status=None,
        sprint=None,
        milestone=None,
        owner=None,
        search=None,
        include_followups=True,
        include_questions=True,
    )


def test_a_run_scoped_read_returns_the_worktree_version_not_main(run_scoped):
    fixture = run_scoped
    main_version = _version(fixture["main"], PROJECT, SLUG)
    worktree_version = _version(fixture["worktree"], PROJECT, SLUG)
    assert worktree_version != main_version

    result = _read(project=PROJECT)

    assert result["version"] == worktree_version, result
    assert "worktree copy" in str(result["data"].get("title", ""))


def test_a_run_scoped_read_of_another_project_comes_from_main(run_scoped):
    """A read of a project the run does not own has no worktree copy, so it is main."""
    fixture = run_scoped
    result = _read(project=OTHER_PROJECT)

    assert result["version"] == _version(fixture["other_repo"], OTHER_PROJECT, SLUG)


def test_a_run_scoped_roadmap_reads_the_worktree_not_main(run_scoped):
    """The roadmap tool resolves the run scope too, and its inventory shows it.

    A count discriminates here rather than a field: the worktree holds a plan
    main does not have, so a roadmap that resolved main would report one plan
    fewer. The explicit-``checkout_path`` arm is the control: it must report
    main's count, which proves the assertion separates the two trees.
    """
    fixture = run_scoped

    scoped = mcp_module._roadmap_tool(PROJECT)
    explicit_main = mcp_module._roadmap_tool(
        PROJECT, checkout_path=str(fixture["main"])
    )

    assert scoped["completion"]["plans"] == 2, scoped["completion"]
    assert explicit_main["completion"]["plans"] == 1, explicit_main["completion"]


def test_a_run_scoped_audit_reads_the_worktree_not_main(run_scoped):
    """The audit tool resolves the run scope too; its checked count shows it,
    with the explicit-``checkout_path`` arm as the control."""
    fixture = run_scoped

    scoped = mcp_module._audit_tool(PROJECT)
    explicit_main = mcp_module._audit_tool(PROJECT, checkout_path=str(fixture["main"]))

    assert scoped["state"]["checked"] == 2, scoped["state"]
    assert explicit_main["state"]["checked"] == 1, explicit_main["state"]


def test_a_run_scoped_resource_read_resolves_to_the_worktree(run_scoped):
    """The typed-resource branch resolves the run scope from the resource's project.

    The plan read is one main does not have, so a resolution that fell back to
    main could not return it at all.
    """
    fixture = run_scoped

    scoped = _resource_read(WORKTREE_ONLY_SLUG)
    assert scoped["version"] == _version(
        fixture["worktree"], PROJECT, WORKTREE_ONLY_SLUG
    )
    assert scoped["provenance"]["checkout"] == str(fixture["worktree"]), scoped

    explicit_main = _resource_read(
        WORKTREE_ONLY_SLUG, checkout_path=str(fixture["main"])
    )
    assert explicit_main.get("ok") is False, explicit_main


def test_a_coordinator_read_without_a_run_is_unchanged(run_scoped, monkeypatch):
    fixture = run_scoped
    monkeypatch.delenv("RECKON_RUN_ID", raising=False)

    result = _read(project=PROJECT)

    assert result["version"] == _version(fixture["main"], PROJECT, SLUG)


def test_an_explicit_checkout_path_read_is_untouched_by_the_run_scope(run_scoped):
    """A caller that passes checkout_path keeps today's behaviour."""
    fixture = run_scoped
    result = _read(project=PROJECT, checkout_path=str(fixture["main"]))

    assert result["version"] == _version(fixture["main"], PROJECT, SLUG)


def test_a_run_scoped_write_lands_in_the_worktree_when_the_copies_are_identical(
    run_scoped_identical,
):
    """The escape the scope closes, on an unmodified worktree copy.

    A copy that has diverged from main's shows which file was written whichever
    path resolved, so it cannot prove the path was scoped. Here the two files
    start byte-identical: only a write that resolved the worktree can leave
    main untouched while the worktree's copy advances.
    """
    fixture = run_scoped_identical
    assert _sha(fixture["main_plan"]) == _sha(fixture["worktree_plan"])
    before_main = _sha(fixture["main_plan"])
    main_version = _version(fixture["main"], PROJECT, SLUG)

    result = mcp_module._edit_plan(
        project=PROJECT,
        slug=SLUG,
        ops=[{"op": "set", "path": "title", "value": "written by the worker"}],
        expected_version=main_version,
        checkout_path=None,
    )

    assert result["ok"] is True, result
    assert fixture["worktree"] in Path(result["path"]).parents
    assert _version(fixture["worktree"], PROJECT, SLUG) == main_version + 1
    assert _version(fixture["main"], PROJECT, SLUG) == main_version
    assert _sha(fixture["main_plan"]) == before_main
    assert _sha(fixture["worktree_plan"]) != _sha(fixture["main_plan"])


def test_a_run_scoped_granular_write_to_another_project_is_refused(run_scoped):
    """A plan-writing entry point with no checkout path cannot escape the worktree."""
    fixture = run_scoped
    before = _sha(fixture["other_plan"])

    result = mcp_module._set_status(
        OTHER_PROJECT,
        SLUG,
        "active",
        expected_version=_version(fixture["other_repo"], OTHER_PROJECT, SLUG),
    )

    assert result["ok"] is False, result
    assert result["error"] == "run_scoped_write"
    assert RUN_ID in result["message"]
    assert str(fixture["other_plan"]) in result["message"]
    assert _sha(fixture["other_plan"]) == before


def test_a_run_scoped_granular_write_to_the_runs_own_project_is_refused(run_scoped):
    """A granular mutator is refused even for the run's own project.

    It is refused rather than redirected, and the expected version here is the
    one the main checkout holds — the version an unguarded write would have
    accepted. So the refusal is the guard speaking, not a version conflict
    returned on the way to a write that would otherwise have landed.

    The redirect is not available to these entry points: ``patch_plan`` and its
    siblings read and write with no checkout root, so a run's own project could
    only be reached by widening the write to main. The registered entry point
    accepts a root and lands in the worktree; here, where it cannot, the write
    stops.
    """
    fixture = run_scoped
    before_main = _sha(fixture["main_plan"])
    before_worktree = _sha(fixture["worktree_plan"])
    before_version = _version(fixture["main"], PROJECT, SLUG)

    result = mcp_module._set_status(
        PROJECT, SLUG, "active", expected_version=before_version
    )

    assert result["ok"] is False, result
    assert result["error"] == "run_scoped_write"
    assert RUN_ID in result["message"]
    assert str(fixture["main_plan"]) in result["message"]
    assert _sha(fixture["main_plan"]) == before_main
    assert _sha(fixture["worktree_plan"]) == before_worktree
    assert _version(fixture["main"], PROJECT, SLUG) == before_version


def test_a_coordinator_granular_write_without_a_run_is_unchanged(
    run_scoped, monkeypatch
):
    fixture = run_scoped
    monkeypatch.delenv("RECKON_RUN_ID", raising=False)
    start = _version(fixture["main"], PROJECT, SLUG)

    result = mcp_module._set_status(PROJECT, SLUG, "active", expected_version=start)

    assert result["ok"] is True, result
    assert _version(fixture["main"], PROJECT, SLUG) == start + 1
