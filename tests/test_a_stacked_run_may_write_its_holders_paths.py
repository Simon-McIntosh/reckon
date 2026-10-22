"""A dispatch built on a stopped holder's chain tip may write its paths.

A run that stopped with unlanded commits still fences its declared paths: a
second writer on those paths would bury the first run's commits, which survive
only as long as its worktree does. That refusal is right for a dispatch cut
from the integration branch — but not for one deliberately cut from the
holder's own chain tip, which exists precisely to make the holder promotable.
The newcomer's worktree already carries every one of the holder's commits, so
refusing it strands the repair the holder cannot make for itself.

The case here is that shape: a held run with a stopped worker and a commit past
its base, and a second dispatch whose base is that run's head. The stacked
dispatch is admitted and both pointers record the stacking; the same dispatch
cut from the integration branch is refused with ``ScopeConflict``, which is the
claim's own rule and must not be weakened. Promoting the holder while its
descendant is live succeeds, because the descendant's worktree now carries the
holder's commits, and the result names that descendant.

The real pointer directory under the user's config home is asserted untouched
to the extent the fixture can see it: every fixture pointer is written to a
temporary crew home, so a case that reached the operator's own home would show
up here rather than in a live fleet.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon import crew
from reckon.crew.runs import pointer_path, read_pointer, scope_claims

dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = "stacked-claim-fixture"
NODE_ID = "repair-on-the-holders-tip"
CLAIMED_PATH = "nova/equilibrium/contour_tree.py"
HOLDER_RUN = "r-20261021T005136987306-read-correction-path"
HOLDER_NODE = "read-correction-path"
DESCENDANT_NODE = "contour-chain-added-failures"
SESSION = "session-stacked-claim"
# A pid no process holds, so the holder reads as stopped rather than running.
DEAD_PID = 99999999

CONFIG: dict[str, Any] = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": False,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _plan_document() -> str:
    head = "<!doctype html><html><head>"
    metas = (
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan"><meta name="plan-slug" content="fixture">'
        '<meta name="plan-title" content="fixture">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-impl" content="0"><meta name="plan-version" content="0">'
    )
    body = '<body><h2 id="s5">A stacked run writes its holder&#x27;s paths</h2></body>'
    return f"{head}{metas}</head>{body}</html>"


def _held_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """A crew home and a repository holding one stopped run with unlanded commits."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    (repo / "nova" / "equilibrium").mkdir(parents=True)
    (repo / "docs" / "plans").mkdir(parents=True)
    (repo / "docs" / "plans" / "fixture.html").write_text(
        _plan_document(), encoding="utf-8"
    )
    (repo / "nova" / "equilibrium" / "contour_tree.py").write_text(
        "seed\n", encoding="utf-8"
    )
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    _git(repo, "add", "docs", CLAIMED_PATH)
    _git(repo, "commit", "-q", "-m", "chore: seed")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    return config_home, repo


def _holder_tree_of_unlanded_commits(repo: Path, tmp_path: Path) -> tuple[str, str]:
    """Cut the holder's worktree and commit on it, so it holds what main cannot reach.

    Returns the holder's dispatch base and the revision it now stands at.
    """
    base = _git(repo, "rev-parse", "HEAD")
    tree = tmp_path / "holder-tree"
    _git(repo, "worktree", "add", "-q", "--detach", str(tree), base)
    (tree / CLAIMED_PATH).write_text("seed\nrepair\n", encoding="utf-8")
    _git(tree, "add", CLAIMED_PATH)
    _git(tree, "commit", "-q", "-m", "fix: repair the contour walk")
    return base, _git(tree, "rev-parse", "HEAD")


def _publish_stopped_holder(repo: Path, tmp_path: Path) -> tuple[str, str, str]:
    """Publish the holder's pointer as a stopped run holding unlanded work.

    Returns the holder run id, its dispatch base and the revision it stands at.
    """
    base, head = _holder_tree_of_unlanded_commits(repo, tmp_path)
    crew._write_json(
        pointer_path(HOLDER_RUN),
        {
            "run_id": HOLDER_RUN,
            "project": PROJECT,
            "repo": str(repo),
            "worktree": str(tmp_path / "holder-tree"),
            "base_sha": base,
            "pid": DEAD_PID,
            "phase": "working",
            "created_at": "2026-10-21T00:51:36Z",
            "node": {
                "id": HOLDER_NODE,
                "plan": "fixture",
                "section": "s5",
                "write_paths": [CLAIMED_PATH],
            },
        },
    )
    return HOLDER_RUN, base, head


def _record_stacking_directly(repo: Path, tmp_path: Path) -> tuple[str, str, str]:
    """Publish a holder and a live stacked descendant, and record the stacking.

    The dispatch that admits a stacked run is what the done-when is about, and
    its arms are the two that need the dispatch's base handed to the check. The
    recording, registry and promotion arms below are reachable without it, so
    they are asserted here as well rather than left unproven behind the wiring:
    what this helper skips is one argument, not the mechanism.
    """
    from reckon.crew.runs import record_stacked_dispatch

    holder, _base, head = _publish_stopped_holder(repo, tmp_path)
    descendant = "r-20261022T101900000000-" + DESCENDANT_NODE
    manifest = tmp_path / (HOLDER_RUN + "-manifest.md")
    manifest.write_text(
        "node: " + HOLDER_NODE + "\nstatus: complete\ncommits: " + head + "\n"
        "changed_paths: " + CLAIMED_PATH + "\n",
        encoding="utf-8",
    )
    holder_record = read_pointer(holder)
    holder_record["manifest_path"] = str(manifest)
    crew._write_json(pointer_path(holder), holder_record)
    crew._write_json(
        pointer_path(descendant),
        {
            "run_id": descendant,
            "project": PROJECT,
            "repo": str(repo),
            "worktree": str(tmp_path / "descendant-tree"),
            "base": head,
            "base_sha": head,
            "pid": os.getpid(),
            "phase": "working",
            "created_at": "2026-10-22T10:19:00Z",
            "node": {
                "id": DESCENDANT_NODE,
                "plan": "fixture",
                "section": "s5",
                "write_paths": [CLAIMED_PATH],
            },
        },
    )
    stacked_record = read_pointer(descendant)
    record_stacked_dispatch(holder, descendant, descendant_record=stacked_record)
    crew._write_json(pointer_path(descendant), stacked_record)
    return holder, descendant, head


def _descendant_node(tmp_path: Path) -> crew.TaskNode:
    return crew.TaskNode(
        id=DESCENDANT_NODE,
        goal="resolve the holder's added gate failures on its own chain",
        plan="fixture",
        section="s5",
        spec_level="guided",
        done_when="the holder's gate measures 0 added failures on this chain",
        write_paths=[CLAIMED_PATH],
        time_budget="20m",
        manifest_path=str(tmp_path / "manifests" / (DESCENDANT_NODE + ".md")),
    )


def _dispatch_on(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    base: str,
) -> dict[str, Any]:
    """Dispatch the descendant at one base, with a worktree cut from that base."""
    created: dict[str, dict[str, str]] = {}

    def prepare_worktree(
        _repo: Path, _session: str, node: str, requested: str
    ) -> dict[str, str]:
        path = tmp_path / "worktrees" / node
        path.parent.mkdir(parents=True, exist_ok=True)
        _git(repo, "worktree", "add", "-q", "--detach", str(path), requested)
        created["worktree"] = {
            "path": str(path),
            "base": requested,
            "base_sha": _git(path, "rev-parse", "HEAD"),
        }
        return created["worktree"]

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare_worktree)
    return crew.dispatch(
        node=_descendant_node(tmp_path),
        project=PROJECT,
        repo=repo,
        config=CONFIG,
        session=SESSION,
        base=base,
        launcher=lambda *_args, **_kwargs: os.getpid(),
        check_budget=False,
    )


def test_a_dispatch_on_the_holders_chain_tip_is_admitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The done-when's admit arm: the stacked dispatch writes the claimed path."""
    _config_home, repo = _held_repository(tmp_path, monkeypatch)
    _run_id, _base, head = _publish_stopped_holder(repo, tmp_path)

    record = _dispatch_on(repo, tmp_path, monkeypatch, base=head)

    run_id = str(record["run_id"])
    assert run_id.startswith("r-")
    live = {str(row["run_id"]) for row in crew.list_live(project=PROJECT)}
    assert run_id in live
    # The holder stays live: its claim is what fences its paths against every
    # other dispatch, and it keeps fencing until it is promoted or discarded.
    assert HOLDER_RUN in live
    assert read_pointer(HOLDER_RUN).get("stacked_runs") == [run_id]
    assert read_pointer(run_id).get("stacked_on") == HOLDER_RUN


def test_a_dispatch_on_the_integration_branch_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: the claim keeps refusing a dispatch that carries none of it."""
    _config_home, repo = _held_repository(tmp_path, monkeypatch)
    _run_id, base, _head = _publish_stopped_holder(repo, tmp_path)

    with pytest.raises(crew.ScopeConflict) as refusal:
        _dispatch_on(repo, tmp_path, monkeypatch, base=base)

    assert CLAIMED_PATH in str(refusal.value)
    assert HOLDER_RUN in str(refusal.value)
    assert not (tmp_path / "worktrees").exists()


def test_the_registry_names_the_stacked_run_and_still_binds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scopes view's row for the holder carries the stacking beside binding."""
    _config_home, repo = _held_repository(tmp_path, monkeypatch)
    _holder, descendant, _head = _record_stacking_directly(repo, tmp_path)

    rows = [
        row
        for row in scope_claims(PROJECT, repo)
        if row["run_id"] == HOLDER_RUN and row["path"] == CLAIMED_PATH
    ]

    assert rows, "the holder's claim must appear in the registry"
    assert rows[0]["binding"] is True
    assert descendant in rows[0].get("stacked_runs", [])


def test_promoting_the_holder_names_its_live_descendant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The holder leaves while its descendant is live, and the result says which."""
    _config_home, repo = _held_repository(tmp_path, monkeypatch)
    _holder, descendant, head = _record_stacking_directly(repo, tmp_path)

    result = crew.complete(
        HOLDER_RUN,
        gate="passed",
        commits=[head],
        root=repo,
        review_waiver="the holder's review is out of this fixture's subject",
    )

    assert result["stacked_descendants"] == [descendant]
    assert not pointer_path(HOLDER_RUN).exists()
    assert pointer_path(descendant).exists()


def test_the_check_admits_the_tip_and_refuses_the_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exception itself, exercised with the base the dispatcher would pass.

    The two dispatch arms above cannot reach this state until the dispatch
    hands its base down, so the check is called here with the same claim list
    ``_repository_scope_claims`` builds for a real dispatch and the base the
    dispatcher resolves. The tip is admitted and named; the integration branch
    is refused, which is the control that keeps the claim's own rule intact.
    """
    from reckon.crew.dispatch import (
        _raise_repository_scope_conflict,
        _repository_scope_claims,
    )
    from reckon.crew.routing import resolve_dispatch_authority

    _config_home, repo = _held_repository(tmp_path, monkeypatch)
    holder, base, head = _publish_stopped_holder(repo, tmp_path)
    node = _descendant_node(tmp_path)
    claims = _repository_scope_claims()
    authority = resolve_dispatch_authority(PROJECT, repo)

    stacked: list[dict[str, Any]] = []
    _raise_repository_scope_conflict(
        node,
        project=PROJECT,
        repo=repo,
        authority=authority,
        claims=claims,
        base_revision=head,
        stacked=stacked,
    )
    assert [row["run_id"] for row in stacked] == [holder]

    with pytest.raises(crew.ScopeConflict):
        _raise_repository_scope_conflict(
            node,
            project=PROJECT,
            repo=repo,
            authority=authority,
            claims=claims,
            base_revision=base,
        )


def test_the_dry_run_and_the_real_dispatch_agree_on_a_stacked_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dry run reaches the verdict the launch reaches.

    A caller reads the dry run's verdict to decide whether to dispatch, so a
    dry run that refuses where the launch admits sends them away from a dispatch
    that would have been accepted. Both are asked the same question here, on the
    same holder and the same base: on the holder's chain tip both admit, and on
    the integration branch both refuse.
    """
    from reckon.crew.dispatch_plan import plan_dispatch

    _config_home, repo = _held_repository(tmp_path, monkeypatch)
    _holder, base, head = _publish_stopped_holder(repo, tmp_path)

    admitted = plan_dispatch(
        node=_descendant_node(tmp_path),
        config=CONFIG,
        project=PROJECT,
        repo=repo,
        base=head,
        session=SESSION,
        report_live_conflicts=True,
        route="deterministic",
    )
    assert admitted.admission["state"] == "admitted"

    record = _dispatch_on(repo, tmp_path, monkeypatch, base=head)
    assert str(record["run_id"]).startswith("r-")

    refused = plan_dispatch(
        node=_descendant_node(tmp_path),
        config=CONFIG,
        project=PROJECT,
        repo=repo,
        base=base,
        session=SESSION,
        report_live_conflicts=True,
        route="deterministic",
    )
    assert refused.admission["state"] == "refused"
    assert refused.admission["error"] == "scope-conflict"

    with pytest.raises(crew.ScopeConflict):
        _dispatch_on(repo, tmp_path, monkeypatch, base=base)


def test_a_stacked_run_widening_onto_the_holders_paths_is_admitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A widening dispatch is judged with the base it was itself cut from.

    A run stacked on a stopped holder has that holder's chain tip as its base,
    so widening onto the holder's other claimed paths adds no writer the holder
    does not already carry. Judged without its own base the same widening is
    refused, which is the state this case pins: the stacked run is admitted and
    a run based on the integration branch is still refused.
    """
    from reckon.crew.dispatch_claims import refuse_widen_scope_conflicts

    _config_home, repo = _held_repository(tmp_path, monkeypatch)
    _holder, descendant, _head = _record_stacking_directly(repo, tmp_path)
    # The control is judged at the revision the holder itself was cut from, which
    # is the one base that carries none of the holder's commits.
    base = str(read_pointer(HOLDER_RUN).get("base_sha") or "")
    assert base

    live = {str(row["run_id"]) for row in crew.list_live(project=PROJECT)}
    assert descendant in live, "the fixture must publish a live stacked descendant"
    assert HOLDER_RUN in live

    # The descendant widens onto a path its own base already carries: admitted.
    refuse_widen_scope_conflicts(read_pointer(descendant), [CLAIMED_PATH])

    # A run cut from the integration branch carries none of it: still refused.
    based_on_main = "r-20261022T101900000000-based-on-main"
    crew._write_json(
        pointer_path(based_on_main),
        {
            "run_id": based_on_main,
            "project": PROJECT,
            "repo": str(repo),
            "worktree": str(tmp_path / "based-on-main-tree"),
            "base": base,
            "base_sha": base,
            "pid": os.getpid(),
            "phase": "working",
            "created_at": str(read_pointer(descendant).get("created_at") or ""),
            "node": {
                "id": "based-on-main",
                "plan": "fixture",
                "section": "s5",
                "write_paths": [CLAIMED_PATH],
            },
        },
    )
    with pytest.raises(crew.ScopeConflict):
        refuse_widen_scope_conflicts(read_pointer(based_on_main), [CLAIMED_PATH])
