"""Tests for refusing a role-scope mismatch at dispatch, not at promotion.

A verifier reads the repository it grades but writes only its own delivery —
manifest, report and logs — which live outside it. Dispatch validates a node
before any worktree exists, so a test-role node that declares repository source
paths is refused there instead of at promotion, where the worker time is already
spent. The dispatch validation and the promotion refusal share one predicate, so
a run that slipped through an earlier dispatch is caught later by the same rule
rather than by a copy that could drift from it.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from reckon import _backends, crew
from reckon.crew import review as review_module
from reckon.crew.node import role_may_write_repository_paths
from reckon.crew.review import review_path, review_store_root
from reckon.crew.runs import (
    _write_json,
    delivery_roots,
    pointer_path,
    reports_dir,
    run_dir,
    runs_dir,
)

PROJECT = "sample"


@pytest.fixture(autouse=True)
def isolated_host_config(monkeypatch, tmp_path):
    """Keep flight resolution off the workstation's real host layer."""
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(tmp_path / "absent" / "flight.yaml"))


@pytest.fixture(autouse=True)
def isolated_temporary_directory(tmp_path, monkeypatch):
    """Keep the granted temp root off the fixture tree.

    Every restricted tier grants the process temp directory, and on this host
    pytest's own tmp_path lives under it — so without this the repository and
    every path beside it would read as reachable and the negative case would
    pass for the wrong reason.
    """
    worker_temp = tmp_path / "worker-temp"
    worker_temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(worker_temp))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point the crew directory at a temp tree, leaving the real one alone."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _node(
    home: Path,
    *,
    role: str,
    write_paths: list[str],
    manifest_path: str | None = None,
) -> crew.TaskNode:
    return crew.TaskNode(
        id="role-scope-node",
        goal="measure the timestamp work against a stated base",
        plan="plan-a",
        section="s1",
        role=role,
        done_when="pytest tests/example.py reports 3 passed",
        write_paths=write_paths,
        time_budget="20m",
        manifest_path=manifest_path or str(run_dir("r-role-scope-node") / "manifest.md"),
        spec_level="exact",
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


@pytest.fixture()
def repository(tmp_path: Path, home: Path) -> Path:
    """A seeded repository whose project is mounted under the temp config home."""
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}),
        encoding="utf-8",
    )
    (root / "source.py").write_text("RESULT = 'sound'\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "source.py"),
        (
            "commit",
            "-q",
            "-m",
            "test: seed verification repository",
            "-m",
            "Provide the stated base for the synthetic verification wave.",
        ),
    ):
        _git(root, *arguments)
    return root


# ── The dispatch gate refuses the mismatch before any work exists ──────────


def test_a_test_role_declaring_repository_paths_is_refused_at_dispatch(home):
    verdict = crew.validate_node(
        _node(
            home,
            role="test",
            write_paths=[
                "pkg/standard_names/graph_ops.py",
                "pkg/schemas/standard_name.yaml",
            ],
        ),
        budget_ceiling="25m",
    )
    assert "scoped" in verdict.failed_properties
    detail = next(f["detail"] for f in verdict.findings if f["property"] == "scoped")
    assert "may not write repository paths" in detail
    assert "pkg/standard_names/graph_ops.py" in detail


def test_a_test_role_with_only_delivery_write_paths_still_dispatches(home):
    run_directory = run_dir("r-role-scope-delivery")
    verdict = crew.validate_node(
        _node(
            home,
            role="test",
            write_paths=[
                str(run_directory),
                str(reports_dir()),
                str(run_directory / "manifest.md"),
            ],
        ),
        budget_ceiling="25m",
    )
    assert verdict.ok, verdict.findings


def test_a_test_role_declaring_its_own_manifest_elsewhere_still_dispatches(
    home, tmp_path
):
    manifest = tmp_path / "elsewhere" / "manifest.md"
    verdict = crew.validate_node(
        _node(
            home,
            role="test",
            write_paths=[str(manifest)],
            manifest_path=str(manifest),
        ),
        budget_ceiling="25m",
    )
    assert verdict.ok, verdict.findings


def test_an_implement_role_declaring_repository_source_still_dispatches(home):
    verdict = crew.validate_node(
        _node(home, role="implement", write_paths=["reckon/crew/node.py"]),
        budget_ceiling="25m",
    )
    assert verdict.ok, verdict.findings


def test_dispatch_is_refused_before_a_worktree_exists(home):
    config = {
        "default_backend": "native",
        "backends": {"native": {"launch": "in-harness", "time_budget": "25m"}},
        "roles": {"test": {"execution_capable": True}, "implement": {}},
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }
    refused = crew.plan_dispatch(
        node=_node(home, role="test", write_paths=["source.py"]),
        config=config,
    )
    assert not refused.validation.ok
    assert "scoped" in refused.validation.failed_properties

    accepted = crew.plan_dispatch(
        node=_node(home, role="implement", write_paths=["source.py"]),
        config=config,
    )
    assert accepted.validation.ok, accepted.validation.findings


def test_a_dispatched_test_node_with_no_explicit_path_resolves_the_role_default(
    home,
):
    config = {
        "default_backend": "native",
        "backends": {"native": {"launch": "in-harness", "time_budget": "25m"}},
        "roles": {"test": {"execution_capable": True, "write_paths": ["."]}},
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }
    node = _node(home, role="test", write_paths=[])
    resolution = crew.plan_dispatch(node=node, config=config)

    assert resolution.validation.ok, resolution.validation.findings
    run_directory = crew.run_dir(resolution.run_id)
    for declared in resolution.node.write_paths:
        assert Path(declared).is_relative_to(run_directory)
    assert "scoped" not in resolution.validation.failed_properties


# ── A restricted role reaches the store its own dispatch points it at ──────
# A review node writes exactly one file and it does not live in the repository
# it grades. The dispatch `reckon crew recover` emits as next_action carries
# --role review and that store path, so the sandbox grant has to include the
# review store or the node the tool itself recommends is refused as
# unreachable before a worktree exists. The shared reports root was granted and
# the reviews store was not — the same defect one directory over, and
# unreachable by any amount of correct work on the worker's side.

REVIEW_BACKEND_CONFIG = {
    "default_backend": "worker",
    "backends": {
        "worker": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {
        "review": {"execution_capable": True, "sandbox": "read-only"},
        "implement": {"execution_capable": True, "sandbox": "worktree-full"},
    },
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}

REVIEWED_RUN_ID = "r-20260914T094141156549-flight-declares-meteredness"


@pytest.fixture()
def review_repository(tmp_path: Path, home: Path) -> Path:
    """A seeded repository carrying the plan section a review node names."""
    root = tmp_path / "review-repo"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "package").mkdir()
    (root / "docs" / "plans" / "plan-a.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="plan-a">'
        '<h2 id="s1">Review scope</h2>',
        encoding="utf-8",
    )
    (root / "package" / "input.txt").write_text("input\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs", "package"),
        (
            "commit",
            "-q",
            "-m",
            "chore: seed review repository",
            "-m",
            "Provide the plan section a review dispatch resolves against.",
        ),
    ):
        _git(root, *arguments)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}),
        encoding="utf-8",
    )
    return root


def _review_node(home: Path, *, write_paths: list[str], role: str = "review"):
    return _node(home, role=role, write_paths=write_paths)


def test_the_review_role_writes_the_store_its_own_dispatch_points_at(
    home: Path, review_repository: Path
):
    """The recover-emitted review dispatch validates instead of being refused."""
    store_path = review_path(PROJECT, REVIEWED_RUN_ID)
    assert store_path.is_relative_to(review_store_root())
    assert review_store_root().is_relative_to(home)

    resolution = crew.plan_dispatch(
        node=_review_node(home, write_paths=[str(store_path)]),
        config=REVIEW_BACKEND_CONFIG,
        project=PROJECT,
        repo=review_repository,
    )

    detail = " ".join(f["detail"] for f in resolution.validation.findings)
    assert "scoped" not in resolution.validation.failed_properties, detail
    assert review_store_root().resolve() in resolution.sandbox_write_roots
    assert reports_dir().resolve() in resolution.sandbox_write_roots


def test_a_write_path_under_no_granted_root_is_still_refused(
    home: Path, review_repository: Path
):
    """The grant widens to the review store and to nothing else."""
    resolution = crew.plan_dispatch(
        node=_review_node(home, write_paths=["package/output.json"]),
        config=REVIEW_BACKEND_CONFIG,
        project=PROJECT,
        repo=review_repository,
    )

    assert "scoped" in resolution.validation.failed_properties
    detail = " ".join(f["detail"] for f in resolution.validation.findings)
    assert "package/output.json" in detail
    assert "read-only" in detail


def test_the_worktree_full_tier_keeps_the_repository_unrestricted(
    home: Path, review_repository: Path
):
    """A role that may write the repository is not scoped by the review store."""
    resolution = crew.plan_dispatch(
        node=_review_node(home, role="implement", write_paths=["package/output.txt"]),
        config=REVIEW_BACKEND_CONFIG,
        project=PROJECT,
        repo=review_repository,
    )

    assert resolution.validation.ok, resolution.validation.findings
    assert resolution.sandbox_write_roots is None


def test_every_durable_delivery_root_is_granted_to_a_restricted_tier(tmp_path: Path):
    """One store granted and a sibling withheld is the defect this closes.

    Ranged over ``delivery_roots()`` rather than the live inventory, so a store
    added later fails this check until it is granted too, instead of silently
    joining the set a role cannot reach. The runs root is the one exception and
    is checked through the node's own run directory, because it is deliberately
    narrowed to that directory rather than granted wholesale.
    """
    repository = tmp_path / "worktree"
    run_directory = tmp_path / "run"
    for directory in (repository, run_directory):
        directory.mkdir()
    review_root = review_store_root()
    assert review_root != reports_dir().resolve()
    ungranted = tmp_path / "ungranted"
    ungranted.mkdir()

    for tier in (_backends.READ_ONLY, _backends.WORKSPACE_WRITE):
        roots = _backends.sandbox_write_roots(
            {"sandbox": tier},
            repository=repository,
            run_directory=run_directory,
            reports_directory=reports_dir(),
            review_store_directory=review_root,
        )
        assert roots is not None
        for store in delivery_roots():
            if store == runs_dir().resolve():
                continue
            assert _backends.sandbox_can_write(
                store, repository=repository, write_roots=roots
            ), f"{tier} withholds {store}"
        assert _backends.sandbox_can_write(
            run_directory, repository=repository, write_roots=roots
        )
        assert not _backends.sandbox_can_write(
            ungranted / "artifact.md", repository=repository, write_roots=roots
        )
    assert (
        _backends.sandbox_write_roots(
            {"sandbox": _backends.WORKTREE_FULL},
            repository=repository,
            run_directory=run_directory,
            reports_directory=reports_dir(),
            review_store_directory=review_root,
        )
        is None
    )


# ── The role predicate is the single spelling of the rule ──────────────────


def test_the_role_predicate_is_the_single_spelling_of_the_rule():
    assert not role_may_write_repository_paths("test")
    assert role_may_write_repository_paths("implement")
    assert role_may_write_repository_paths("")


def _write_test_pointer(
    *,
    run_id: str,
    repository: Path,
    base: str,
    manifest: Path,
    write_paths: list[str],
    role: str = "test",
) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": base,
            "launch": "in-harness",
            "role": role,
            "backend": "native",
            "created_at": "2026-09-04T10:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": "merged-head-verification",
                "plan": "fixture",
                "section": "verification",
                "role": role,
                "time_budget": "30m",
                "write_paths": write_paths,
            },
        },
    )


def _stored_review(run_id: str) -> None:
    """Attach a complete independent review so the review gate is satisfied.

    A passing run that changed the repository is refused at promotion until a
    complete review is stored, and that refusal is not what these tests
    measure: one asserts the role-scope refusal that sits behind it, the other
    the landing acceptance. Storing the review is setup, not subject.
    """
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
        }
    )
    review_module.store_review(record)


def test_a_test_run_that_slipped_through_is_still_refused_at_promotion(
    repository,
    tmp_path,
):
    base = _git(repository, "rev-parse", "HEAD")
    (repository / "source.py").write_text("RESULT = 'edited by verifier'\n")
    _git(repository, "add", "source.py")
    _git(
        repository,
        "commit",
        "-q",
        "-m",
        "test: change graded source",
        "-m",
        "Exercise the promotion boundary for a verifier-owned commit.",
    )
    commit = _git(repository, "rev-parse", "HEAD")
    manifest = tmp_path / "source-edit-manifest.md"
    manifest.write_text(
        "node: merged-head-verification\n"
        "status: complete\n"
        f"commits: {commit}\n"
        "changed_paths: source.py\n"
        "tests: source-edit refusal exercised\n",
        encoding="utf-8",
    )
    run_id = "r-role-scope-slipped-through"
    _write_test_pointer(
        run_id=run_id,
        repository=repository,
        base=base,
        manifest=manifest,
        write_paths=["source.py"],
    )
    _stored_review(run_id)

    with pytest.raises(crew.CrewError, match="verifier may read"):
        crew.complete(
            run_id,
            gate="passed",
            commits=[commit],
            root=repository,
        )

    assert pointer_path(run_id).is_file()


# ── The landing contract and the write fence agree ─────────────────────────
# A worker told to append its landing record and evidence anchor is granted
# those paths — the plan file and the cumulative evidence record — without the
# coordinator naming them, and only a role whose sandbox lets it write the
# worktree (and so composes the contract) receives them. They are shared by
# every node on the plan, so on a promotion the run that wrote both is no
# longer refused for undeclared companions.

PLAN_FILE = "docs/plans/plan-a.html"
EVIDENCE_RECORD = "docs/evidence/archive/plan-a-landed.html"
PLAN_SECTION = "landing-fence"


def _landing_plan(project: str = "sample") -> str:
    return (
        "<!doctype html>"
        "<html><head>"
        f'<meta name="docs-project" content="{project}">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="plan-a">'
        '<meta name="plan-status" content="active">'
        f'<h2 id="{PLAN_SECTION}">Landing fence</h2>'
        "</head></html>"
    )


READ_ONLY_ROLES_CONFIG = {
    "default_backend": "worker",
    "backends": {
        "worker": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {
        "review": {"execution_capable": True, "sandbox": "read-only"},
        "investigate": {"execution_capable": True, "sandbox": "read-only"},
    },
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


def test_an_implement_role_receives_the_landing_paths_in_its_write_scope(
    home: Path, review_repository: Path
):
    """The dispatch payload carries the plan and evidence paths un-named."""
    deliverable = f"package/{PLAN_SECTION}.py"
    resolution = crew.plan_dispatch(
        node=_node(
            home,
            role="implement",
            write_paths=[deliverable],
            manifest_path=str(run_dir("r-landing-implement") / "manifest.md"),
        ),
        config=REVIEW_BACKEND_CONFIG,
        project=PROJECT,
        repo=review_repository,
    )
    assert resolution.validation.ok, resolution.validation.findings
    declared = list(resolution.as_dict()["write_paths"])
    assert PLAN_FILE in declared
    assert EVIDENCE_RECORD in declared
    assert deliverable in declared
    assert set(resolution.node.write_paths) == set(declared)


@pytest.mark.parametrize("role", ["review", "investigate"])
def test_a_read_only_role_does_not_receive_the_landing_paths(
    home: Path, review_repository: Path, role: str
):
    """The negative: a role whose worker never composes the contract gains nothing."""
    store_path = review_path(PROJECT, REVIEWED_RUN_ID)
    resolution = crew.plan_dispatch(
        node=_node(
            home,
            role=role,
            write_paths=[str(store_path)],
        ),
        config=READ_ONLY_ROLES_CONFIG,
        project=PROJECT,
        repo=review_repository,
    )
    assert resolution.validation.ok, resolution.validation.findings
    declared = list(resolution.node.write_paths)
    assert PLAN_FILE not in declared
    assert EVIDENCE_RECORD not in declared


# The local lane runs a different dialect than the metered codex lane. The
# landing grant and the contract gates must not notice: a read-only role stays
# read-only, an implement role stays repo-writing, whatever command names the
# backend, because both gates are keyed on the sandbox's writability of the
# worktree rather than on which dialect relocates the process directory.
LOCAL_LANE_CONFIG = {
    "default_backend": "worker",
    "backends": {
        "worker": {
            "launch": "cli",
            "command": "clive",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {
        "review": {"execution_capable": True, "sandbox": "read-only"},
        "investigate": {"execution_capable": True, "sandbox": "read-only"},
        "implement": {"execution_capable": True, "sandbox": "worktree-full"},
    },
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


@pytest.mark.parametrize("role", ["review", "investigate"])
def test_a_read_only_role_on_the_local_lane_validates_clean(
    home: Path, review_repository: Path, role: str
):
    """The read-only dry run passes and gains no landing paths on any dialect."""
    store_path = review_path(PROJECT, REVIEWED_RUN_ID)
    run_tag = f"r-local-{role}"
    resolution = crew.plan_dispatch(
        node=_node(
            home,
            role=role,
            write_paths=[str(store_path)],
            manifest_path=str(run_dir(run_tag) / "manifest.md"),
        ),
        config=LOCAL_LANE_CONFIG,
        project=PROJECT,
        repo=review_repository,
    )
    assert resolution.validation.ok, resolution.validation.findings
    assert "scoped" not in resolution.validation.failed_properties
    declared = list(resolution.node.write_paths)
    assert PLAN_FILE not in declared
    assert EVIDENCE_RECORD not in declared


def test_an_implement_role_on_the_local_lane_still_receives_the_landing_paths(
    home: Path, review_repository: Path
):
    """The repo-writing role keeps the landing grant on the local lane too."""
    deliverable = f"package/{PLAN_SECTION}.py"
    resolution = crew.plan_dispatch(
        node=_node(
            home,
            role="implement",
            write_paths=[deliverable],
            manifest_path=str(run_dir("r-local-implement") / "manifest.md"),
        ),
        config=LOCAL_LANE_CONFIG,
        project=PROJECT,
        repo=review_repository,
    )
    assert resolution.validation.ok, resolution.validation.findings
    declared = list(resolution.as_dict()["write_paths"])
    assert PLAN_FILE in declared
    assert EVIDENCE_RECORD in declared
    assert deliverable in declared
    assert set(resolution.node.write_paths) == set(declared)


def test_the_prompt_contract_gate_tracks_writability_not_the_process_directory(
    home,
):
    """Both gates key on the same fact: the worker can write the worktree.

    The landing text is decided by the writability dispatch resolves, so a
    worker parked in a delivery directory but able to write the worktree still
    receives the contract, and one standing in the worktree whose sandbox
    forbids writes does not.
    """
    from reckon.crew.prompts import PLAN_LANDING_CONTRACT

    base = {
        "node": _node(home, role="implement", write_paths=["package/out.py"]),
        "project": "proj",
        "worktree": "/repo/worktrees/run",
        "manifest_path": "/state/runs/run/manifest.md",
        "time_budget": "20m",
        "needs_help_after_failures": 2,
    }
    in_delivery_writable = crew.compose_prompt(
        working_directory="/state/runs/run", can_write_worktree=True, **base
    )
    assert PLAN_LANDING_CONTRACT in in_delivery_writable

    in_tree_read_only = crew.compose_prompt(
        working_directory="/repo/worktrees/run", can_write_worktree=False, **base
    )
    assert PLAN_LANDING_CONTRACT not in in_tree_read_only


def test_the_contract_gate_tracks_worktree_writability(tmp_path):
    """Guard the guard: the grant keys on the sandbox, never the dialect name."""
    import importlib

    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    repository = tmp_path / "worktree"
    run_directory = tmp_path / "run"
    for directory in (repository, run_directory):
        directory.mkdir()

    in_harness_full = {"launch": "in-harness", "sandbox": "worktree-full"}
    codex_worktree = {"launch": "cli", "command": "codex", "sandbox": "worktree-full"}
    clive_worktree = {"launch": "cli", "command": "clive", "sandbox": "worktree-full"}
    in_harness_ro = {"launch": "in-harness", "sandbox": "read-only"}
    codex_read_only = {"launch": "cli", "command": "codex", "sandbox": "read-only"}
    clive_read_only = {"launch": "cli", "command": "clive", "sandbox": "read-only"}

    for backend in (in_harness_full, codex_worktree, clive_worktree):
        assert dispatch_module._can_write_worktree(
            backend, repository=repository, run_directory=run_directory
        )
    for backend in (in_harness_ro, codex_read_only, clive_read_only):
        assert not dispatch_module._can_write_worktree(
            backend, repository=repository, run_directory=run_directory
        )


def _promotion_repository(tmp_path: Path, home: Path) -> Path:
    """A repository whose plan is committed and whose landing paths are clean."""
    root = tmp_path / "promote-root"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}),
        encoding="utf-8",
    )
    (root / "docs" / "plans" / "plan-a.html").write_text(
        _landing_plan(), encoding="utf-8"
    )
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
        (
            "commit",
            "-q",
            "-m",
            "test: seed promotion repository",
            "-m",
            "Provide the committed plan a landing run appends to.",
        ),
    ):
        _git(root, *arguments)
    return root


def test_promoting_a_run_that_wrote_both_landing_paths_is_accepted(
    tmp_path: Path, home: Path
):
    """End to end: the dispatch grant lets the landing run promote unchanged."""
    root = _promotion_repository(tmp_path, home)
    base = _git(root, "rev-parse", "HEAD")
    (root / "docs" / "plans" / "plan-a.html").write_text(
        _landing_plan().replace("landing-fence", "landed-by-run"),
        encoding="utf-8",
    )
    (root / "docs" / "evidence" / "archive").mkdir(parents=True)
    (root / "docs" / "evidence" / "archive" / "plan-a-landed.html").write_text(
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="plan-a-landed">'
        "</head></html>",
        encoding="utf-8",
    )
    _git(root, "add", "docs")
    _git(
        root,
        "commit",
        "-q",
        "-m",
        "test: land plan edit and evidence anchor",
        "-m",
        "Exercise promotion of a run that wrote both landing paths.",
    )
    commit = _git(root, "rev-parse", "HEAD")
    manifest = tmp_path / "landing-manifest.md"
    manifest.write_text(
        "node: landing-run\n"
        "status: complete\n"
        f"commits: {commit}\n"
        "changed_paths: docs/plans/plan-a.html "
        "docs/evidence/archive/plan-a-landed.html\n"
        "tests: landing promotion exercised\n",
        encoding="utf-8",
    )
    run_id = "r-landing-promotion"
    _write_test_pointer(
        run_id=run_id,
        repository=root,
        base=base,
        manifest=manifest,
        write_paths=[PLAN_FILE, EVIDENCE_RECORD],
        role="implement",
    )
    _stored_review(run_id)

    result = crew.complete(
        run_id,
        gate="passed",
        commits=[commit],
        outcome="the plan and cumulative evidence record landed in scope",
        root=root,
    )

    assert result.get("scope_exceptions", []) == []
    assert not pointer_path(run_id).is_file()
