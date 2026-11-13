"""Source declarations meet at git merge; nonmergeable artifacts keep claims."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import (
    claims,
    landing_measure,
    merge,
    promotion_scope,
    queue_admission,
    queue_landing,
    recovery_repair_dispatch,
    runs,
)
from reckon.crew.node import validate_node
from reckon.crew.prompts import compose_prompt
from tests.helpers.git import git
from tests.test_a_queued_run_is_admitted import (
    CONFIG,
    PROJECT,
    _config_for,
    _lane_document,
    _queue,
    admit_repo,  # noqa: F401 - imported fixture
)
from tests.test_a_source_file_is_shared_by_default import (
    harness_handshake,  # noqa: F401 - imported fixture
)

DIRECTORY = "reckon/crew"
SOURCE = "reckon/crew/source.py"
SOURCE_TEXT = (
    "def first():\n    return 1\n" + "\n" * 12 + "def second():\n    return 2\n"
)


@pytest.fixture()
def repository(admit_repo: Path) -> Path:  # noqa: F811 - imported fixture
    (admit_repo / DIRECTORY).mkdir(parents=True)
    (admit_repo / SOURCE).write_text(SOURCE_TEXT)
    git(admit_repo, "add", "--", SOURCE)
    git(
        admit_repo,
        "commit",
        "-q",
        "-m",
        "test: seed source",
        "-m",
        "Provide separated functions for concurrent edits.",
    )
    return admit_repo


def _node(name: str, paths: list[str], *, peers: dict | None = None) -> crew.TaskNode:
    return crew.TaskNode(
        id=name,
        plan="plan-a",
        section="dispatch",
        goal="edit a source function",
        done_when="pytest reports passing source sharing assertions",
        write_paths=paths,
        peer_scopes=peers or {},
        manifest_path=str(runs.run_dir(f"r-manifest-{name}") / "manifest.md"),
        spec_level="exact",
        negative_control="restore the source-directory claim",
        time_budget="20m",
    )


def _dispatch(
    repo: Path, name: str, paths: list[str], *, peers: dict | None = None
) -> dict:
    return crew.dispatch(
        node=_node(name, paths, peers=peers),
        project=PROJECT,
        repo=repo,
        config=CONFIG,
        session=f"coordinator-{name}",
        check_budget=False,
        launcher=lambda *_args, **_kwargs: os.getpid(),
    )


@pytest.mark.parametrize(
    "owner_path,joiner_path",
    [(SOURCE, DIRECTORY), (DIRECTORY, SOURCE), (DIRECTORY, DIRECTORY)],
)
def test_a_directory_and_its_live_file_are_dispatched_and_launched(
    repository: Path,
    owner_path: str,
    joiner_path: str,
) -> None:
    owner = _dispatch(repository, "owner", [owner_path])
    joiner = _dispatch(repository, "joiner", [joiner_path])

    assert owner["run_id"] != joiner["run_id"]
    assert owner["worktree"] != joiner["worktree"]
    for run in (owner, joiner):
        pointer = runs.read_pointer(run["run_id"])
        assert pointer["pid"] == os.getpid()
        assert pointer["phase"] == "starting", pointer


@pytest.mark.parametrize("delivered", [False, True])
def test_a_queued_directory_is_admitted_past_a_file_claim(
    repository: Path, delivered: bool
) -> None:
    owner = _dispatch(repository, "owner", [SOURCE])
    if delivered:
        pointer = runs.read_pointer(owner["run_id"])
        pointer["phase"] = "complete"
        Path(pointer["manifest_path"]).write_text("status: complete\n")
        runs._write_json(runs.pointer_path(owner["run_id"]), pointer)
    config, resolve = _config_for()
    config["backends"]["clive"]["lane_document"] = str(
        _lane_document(repository.parent, worker_slots=2)
    )
    request = _queue(repository, node_id="queued", run_id="r-queued")
    request["node"]["write_paths"] = [DIRECTORY]
    runs.queue_dispatch(request)

    result = queue_admission.admit_once(
        config_for=resolve, launcher=lambda *_args, **_kwargs: os.getpid()
    )

    row = next(row for row in result["verdicts"] if row["run_id"] == "r-queued")
    assert row["verdict"] == "launched", row
    assert result["launched"] == ["r-queued"]
    assert owner["run_id"] not in result["held"]
    assert runs.read_pointer("r-queued")["phase"] == "starting"


def test_a_worker_widens_onto_a_live_source_claim(repository: Path) -> None:
    _dispatch(repository, "owner", [SOURCE])
    joiner = _dispatch(repository, "joiner", ["another.py"])

    pointer, added = claims.widen_claim(
        joiner["run_id"],
        [DIRECTORY],
        self_widen=True,
        reason="The edit needs the source directory.",
    )

    assert added == [DIRECTORY]
    assert DIRECTORY in pointer["node"]["write_paths"]


@pytest.mark.parametrize(
    "path",
    [
        "docs/plans/plan-a.html",
        "docs/evidence/archive/plan-a-landed.html",
        "docs/figures/plan-a",
        "docs/state/generated.json",
        "uv.lock",
        "output.receipt",
    ],
)
def test_an_unmergeable_path_is_refused_naming_its_live_owner(
    repository: Path, path: str
) -> None:
    owner = _dispatch(repository, "owner", [path])

    with pytest.raises(crew.ScopeConflict) as refused:
        _dispatch(repository, "joiner", [path])

    assert refused.value.run_id == owner["run_id"]
    assert owner["run_id"] in str(refused.value)
    request = _queue(repository, node_id="queued", run_id="r-queued")
    request["node"]["write_paths"] = [path]
    hold = queue_admission._live_claim_hold(
        request,
        runs.list_live(project=PROJECT),
        dispositions={},
        shared_files=runs._shared_write_paths(PROJECT, repository),
    )
    assert hold is not None and owner["run_id"] in hold


def test_peer_declarations_are_information_in_validation_and_the_brief(
    repository: Path,
) -> None:
    node = _node("joiner", [SOURCE], peers={"owner": [SOURCE]})
    assert validate_node(node, repository=repository).ok
    prompt = compose_prompt(
        node=node,
        project=PROJECT,
        worktree=str(repository),
        working_directory=str(repository),
        manifest_path=str(repository.parent / "manifest.md"),
        time_budget="20m",
        needs_help_after_failures=2,
        peer_scopes=node.peer_scopes,
    )
    assert f"owner → {SOURCE}" in prompt
    assert "overlapping source and test edits are allowed" in prompt
    assert "never touch their paths" not in prompt
    assert "request a scope change instead" not in prompt
    owner = _dispatch(repository, "owner", [SOURCE])
    joiner = _dispatch(repository, "joiner", [SOURCE], peers={"owner": [SOURCE]})
    assert joiner["run_id"] != owner["run_id"]


@pytest.mark.parametrize(
    "paths",
    [
        ("future", "future/module.py"),
        (DIRECTORY, SOURCE),
        ("tests", "tests/source.py"),
        ("docs/plans", "docs/plans/renderer.py"),
    ],
)
def test_source_sharing_does_not_need_a_root_declaration_or_existing_directory(
    repository: Path, paths: tuple[str, str]
) -> None:
    declaration = repository / "docs/state" / PROJECT / "shared-write-paths.json"
    declaration.write_text(json.dumps({"shared_roots": []}))
    _dispatch(repository, "owner", [paths[1]])
    assert _dispatch(repository, "joiner", [paths[0]])["run_id"]


def _edit(run: dict, old: str, new: str) -> str:
    tree = Path(run["worktree"])
    target = tree / SOURCE
    target.write_text(target.read_text().replace(old, new))
    git(tree, "add", "--", SOURCE)
    git(
        tree,
        "commit",
        "-q",
        "-m",
        "fix: change source function",
        "-m",
        "Deliver the function edit from an isolated worktree.",
    )
    return git(tree, "rev-parse", "HEAD")


@pytest.mark.parametrize("conflicting", [False, True])
def test_shared_edits_land_or_return_the_real_conflict_for_repair(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conflicting: bool
) -> None:
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-q", str(origin))
    git(repository, "remote", "add", "origin", str(origin))
    git(repository, "push", "-q", "-u", "origin", "main")
    owner = _dispatch(repository, "owner", [SOURCE])
    joiner = _dispatch(repository, "joiner", [SOURCE])
    base = git(repository, "rev-parse", "HEAD")
    first = _edit(owner, "return 1", "return 11")
    second = _edit(joiner, "return 1" if conflicting else "return 2", "return 22")
    merge.land_merge(
        repository,
        head=first,
        message="fix: land first edit",
        body="Preserve the first isolated edit.",
        release=False,
    )
    if not conflicting:
        merge.land_merge(
            repository,
            head=second,
            message="fix: land second edit",
            body="Combine the separate function edits.",
            release=False,
        )
        content = git(repository, "show", f"HEAD:{SOURCE}")
        assert "return 11" in content and "return 22" in content
        assert git(origin, "rev-parse", "refs/heads/main") == git(
            repository, "rev-parse", "HEAD"
        )
        return
    measured = landing_measure.Measurement(
        command="true",
        suite={},
        population={"main_sha": base, "candidate_sha": second},
        head=second,
        project=PROJECT,
    )
    monkeypatch.setattr(
        queue_landing, "_measurement_record", lambda _run: measured.as_dict()
    )
    monkeypatch.setattr(
        queue_landing, "_moved_primary_keeps_the_measurement", lambda *_args: True
    )
    repairs = []
    monkeypatch.setattr(
        recovery_repair_dispatch,
        "dispatch_merge_repair",
        lambda pointer, **details: repairs.append((pointer["run_id"], details)),
    )
    with pytest.raises(crew.CrewError, match="no longer merges cleanly"):
        queue_landing._verified_measurement(joiner["run_id"])
    assert repairs == [
        (
            joiner["run_id"],
            {
                "head": second,
                "primary": git(repository, "rev-parse", "HEAD"),
                "paths": [SOURCE],
            },
        )
    ]
    assert "return 11" in git(repository, "show", f"HEAD:{SOURCE}")
    assert "return 22" not in git(repository, "show", f"HEAD:{SOURCE}")


def test_a_source_dispatch_built_on_a_holder_records_its_chain(
    repository: Path,
) -> None:
    owner = _dispatch(repository, "owner", [SOURCE])
    head = _edit(owner, "return 1", "return 11")
    pointer = runs.read_pointer(owner["run_id"])
    pointer["pid"] = 99999999
    pointer["process_alive"] = False
    Path(pointer["manifest_path"]).write_text("status: blocked\n")
    runs._write_json(runs.pointer_path(owner["run_id"]), pointer)
    joiner = crew.dispatch(
        node=_node("joiner", [SOURCE]),
        project=PROJECT,
        repo=repository,
        config=CONFIG,
        session="coordinator-joiner",
        base=head,
        check_budget=False,
        launcher=lambda *_args, **_kwargs: os.getpid(),
    )
    assert joiner["stacked_on"] == owner["run_id"]
    assert joiner["run_id"] in runs.read_pointer(owner["run_id"])["stacked_runs"]


def test_a_sharing_declaration_cannot_override_a_plan_claim(repository: Path) -> None:
    path = "docs/plans/plan-a.html"
    declaration = repository / "docs/state" / PROJECT / "shared-write-paths.json"
    declaration.write_text(json.dumps({"paths": [path], "shared_roots": []}))
    owner = _dispatch(repository, "owner", [path])
    with pytest.raises(crew.ScopeConflict) as refused:
        _dispatch(repository, "joiner", [path])
    assert refused.value.run_id == owner["run_id"]


@pytest.mark.parametrize("claim", [SOURCE, DIRECTORY])
def test_promotion_acceptance_never_holds_on_a_source_peer(
    repository: Path, claim: str
) -> None:
    _dispatch(repository, "owner", [claim])
    joiner = _dispatch(repository, "joiner", ["another.py"])
    accepted = promotion_scope._accepted_scope_exceptions(
        joiner["run_id"],
        [SOURCE],
        {SOURCE: "Companion source edit."},
        record=runs.read_pointer(joiner["run_id"]),
        tree=Path(joiner["worktree"]),
        commits=[],
    )
    assert accepted == [{"path": SOURCE, "reason": "Companion source edit."}]


@pytest.mark.parametrize(
    "declaration",
    ["{", json.dumps({"shared_roots": [], "exclusive_patterns": ["reckon/crew/"]})],
)
def test_a_sharing_declaration_cannot_reintroduce_a_source_hold(
    repository: Path, declaration: str
) -> None:
    policy = repository / "docs/state" / PROJECT / "shared-write-paths.json"
    policy.write_text(declaration)
    _dispatch(repository, "owner", [SOURCE])
    assert _dispatch(repository, "joiner", [DIRECTORY])["run_id"]
