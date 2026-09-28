"""Dispatch grants every node its own landing fragment, never a shared record.

A node's landing record once landed on three paths shared by every node on its
plan: the plan HTML, the plan's cumulative evidence record, and the plan-wide
figure directory. Two nodes on one plan therefore wrote the same files and the
merge conflict was structural rather than accidental. Dispatch now grants each
node a fragment of its own -- ``docs/evidence/fragments/<plan>/<node>.html``
and ``docs/figures/<plan>/<node>/`` -- derived from the dry run the same way a
real dispatch derives it, so this module judges the shipped default rather than
a copy of it.

The disjointness measure is pairwise in both directions: two node scopes must
share no path, and no scope may be nested inside another. A shared evidence
record reintroduced into the default scope breaks both directions at once, and
the declared negative control exercises exactly that.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from itertools import combinations, product
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import runs

dispatch = importlib.import_module("reckon.crew.dispatch")

PLAN = "scoring-should-stop-a-promotion"


CONFIG = {
    "default_backend": "alpha",
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
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def isolated_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """Build the smallest mounted plan repository a dry run can resolve against."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    plan = repo / "docs" / "plans" / f"{PLAN}.html"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        f"""<!doctype html>
<html><head>
<meta name="docs-project" content="sample">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{PLAN}">
</head><body><h2 id="landing">Landing record</h2></body></html>
""",
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", str(plan.relative_to(repo))],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({"sample": str(repo / "docs")}), encoding="utf-8"
    )
    watcher = {"arming_line": "reckon crew watch --project sample", "pid": 7319}

    def watch_state(_project: str, *, session: str | None = None) -> dict:
        delivery = (
            runs.follower_state(_project, session) if session is not None else None
        )
        return {
            "arming_line": watcher["arming_line"],
            "watcher_live": bool(watcher.get("live")),
            "watcher": {"pid": watcher["pid"]} if watcher.get("live") else {},
            "attach_line": runs._watch_attach_line(_project, session=session),
            "session": session,
            "session_attached": None if delivery is None else bool(delivery["live"]),
            "follower": {} if delivery is None else delivery["follower"],
        }

    def ensure_watch(_project: str, *, session: str | None = None) -> dict:
        watcher["live"] = True
        return watch_state(_project, session=session)

    monkeypatch.setattr(dispatch, "watch_state", watch_state)
    monkeypatch.setattr(dispatch, "_ensure_watch_producer", ensure_watch)
    return config_home, repo


def _dry_run(
    config_home: Path,
    repo: Path,
    name: str,
    *,
    write_paths: list[str] | None = None,
) -> tuple[crew.TaskNode, dict]:
    """Resolve one node the way a real dispatch would, without side effects."""
    node = crew.TaskNode(
        id=f"node-{name}",
        goal="record one plan landing",
        plan=PLAN,
        section="landing",
        role="implement",
        spec_level="guided",
        done_when="pytest reports one passing landing-path guard case",
        write_paths=list(write_paths)
        if write_paths is not None
        else [f"src/{name}.py"],
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / f"{name}.md"),
    )
    plan = dispatch.plan_dispatch(
        node=node,
        config=CONFIG,
        project="sample",
        repo=repo,
        base="HEAD",
    )
    return node, plan.as_dict()


def _contains(outer: str, inner: str) -> bool:
    """Whether ``inner`` is ``outer`` or lies beneath it."""
    return outer == inner or inner.startswith(outer.rstrip("/") + "/")


def _overlapping_pairs(scopes: list[list[str]]) -> list[tuple[int, int, str, str]]:
    """Every ordered pair of paths, from two scopes, that nests either way."""
    overlaps: list[tuple[int, int, str, str]] = []
    for left, right in combinations(range(len(scopes)), 2):
        overlaps.extend(
            (left, right, one, other)
            for one, other in product(scopes[left], scopes[right])
            if _contains(one, other) or _contains(other, one)
        )
    return overlaps


SIX = 6


def test_six_default_scopes_on_one_plan_are_pairwise_disjoint(
    isolated_project: tuple[Path, Path],
) -> None:
    """The default cannot make two nodes on one plan write the same path."""
    config_home, repo = isolated_project

    scopes = [
        _dry_run(config_home, repo, f"n{index}")[1]["write_paths"]
        for index in range(SIX)
    ]

    assert _overlapping_pairs(scopes) == []


def test_default_scope_names_the_node_fragment_and_no_shared_record(
    isolated_project: tuple[Path, Path],
) -> None:
    """A node gets its own fragment; the three shared landing paths are gone."""
    config_home, repo = isolated_project

    _node, record = _dry_run(config_home, repo, "fragment")
    write_paths = record["write_paths"]

    assert f"docs/evidence/fragments/{PLAN}/node-fragment.html" in write_paths
    assert f"docs/figures/{PLAN}/node-fragment" in write_paths

    assert f"docs/evidence/archive/{PLAN}-landed.html" not in write_paths
    assert f"docs/figures/{PLAN}" not in write_paths
    assert f"docs/plans/{PLAN}.html" not in write_paths


def test_a_redispatched_node_gets_the_same_fragment_paths(
    isolated_project: tuple[Path, Path],
) -> None:
    """The fragment is derived from the node id, so a redispatch is stable."""
    config_home, repo = isolated_project

    first = _dry_run(config_home, repo, "redispatched")[1]["write_paths"]
    second = _dry_run(config_home, repo, "redispatched")[1]["write_paths"]

    fragments = {
        path
        for path in first
        if path.startswith(("docs/evidence/fragments/", "docs/figures/"))
    }
    assert fragments
    assert fragments <= set(second)


def test_an_explicitly_declared_shared_record_is_granted_with_a_warning(
    isolated_project: tuple[Path, Path],
) -> None:
    """A coordinator who declares the cumulative record still gets it, warned."""
    config_home, repo = isolated_project
    shared = f"docs/evidence/archive/{PLAN}-landed.html"

    _node, record = _dry_run(config_home, repo, "shared", write_paths=[shared])

    assert shared in record["write_paths"]
    assert any(
        shared in warning and "reintroduces the merge conflict" in warning
        for warning in record["warnings"]
    ), record["warnings"]
    # The default fragment is granted alongside the explicit declaration.
    assert f"docs/evidence/fragments/{PLAN}/node-shared.html" in record["write_paths"]


def test_a_composed_prompt_names_the_fragment_and_no_shared_record(
    isolated_project: tuple[Path, Path],
) -> None:
    """The carrier prompt shows the fragment among the node's write paths."""
    config_home, repo = isolated_project
    from reckon.crew.prompts import compose_prompt

    node, _record = _dry_run(config_home, repo, "prompt")
    prompt = compose_prompt(
        node=node,
        project="sample",
        worktree=str(repo),
        working_directory=str(repo),
        manifest_path=str(config_home / "manifests" / "prompt.md"),
        time_budget="20m",
        needs_help_after_failures=2,
    )

    assert f"docs/evidence/fragments/{PLAN}/node-prompt.html" in prompt
    assert f"docs/evidence/archive/{PLAN}-landed.html" not in prompt
    assert f'docs/figures/{PLAN}"' not in prompt


def test_the_shared_set_still_describes_the_three_legacy_landing_paths(
    isolated_project: tuple[Path, Path],
) -> None:
    """The exemption set remains the three paths a declaration is judged against."""
    _config_home, repo = isolated_project
    node = crew.TaskNode(
        id="node-legacy",
        goal="record one plan landing",
        plan=PLAN,
        section="landing",
        role="implement",
        spec_level="guided",
        done_when="pytest reports one passing landing-path guard case",
        write_paths=[],
        time_budget="20m",
    )
    authority = {"plan": {"docs": str(repo / "docs"), "repository": str(repo)}}

    paths = dispatch._shared_landing_paths(node, project="sample", authority=authority)

    assert (
        repo / "docs" / "evidence" / "archive" / f"{PLAN}-landed.html"
    ).resolve() in paths
    assert (repo / "docs" / "figures" / PLAN).resolve() in paths
