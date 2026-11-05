"""A named anchor is a path the worker is pointed at without being handed it.

A brief sometimes has to name a large file — the record it must find its way
to, or the module it must stay away from — and until now every named path cost
the flat reference charge whether or not the dispatcher meant the worker to
read it. ``--anchor-path`` states the opt-out: the path stays on the record so a
later reader sees the worker was briefed about a file it was not given, and it
enters the estimate at nothing rather than at a reference cost. A path that is
both anchored and declared a write path is refused with the path named, because
a node cannot both edit a file and be briefed without reading it.
"""

from __future__ import annotations

import json
import math
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew, crew_dispatch_commands, ledger
from reckon.crew import routing
from reckon.crew.node import validate_node
from reckon.crew.routing import _REFERENCE_COST_TOKENS, _context_file_inputs

PROJECT = "proj"
PLAN = "plan-a"
LARGE_PATH = "docs/research/large_record.html"
WRITE_PATH = "docs/research/landed_record.html"
LARGE_BYTES = 1_000_000
CONFIG = {
    "default_backend": "worker",
    "backends": {
        "worker": {
            "launch": "cli",
            "command": "worker",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}
DONE_WHEN = (
    "pytest reports the census charged the anchored path nothing; "
    f"the record lives at {LARGE_PATH}"
)


def _node(
    *, write_paths: list[str], anchor_paths: list[str], done_when: str = DONE_WHEN
) -> crew.TaskNode:
    return crew.TaskNode(
        id=PLAN,
        goal="name an anchor without loading it",
        plan=PLAN,
        section="s1",
        role="implement",
        spec_level="exact",
        done_when=done_when,
        write_paths=write_paths,
        anchor_paths=anchor_paths,
        time_budget="20m",
    )


def _authority(repo: Path) -> dict[str, object]:
    return {
        "plan": {
            "project": PROJECT,
            "repository": str(repo),
            "docs": str(repo / "docs"),
        }
    }


def _seed_large_file(repo: Path) -> None:
    target = repo / LARGE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x" * LARGE_BYTES)


def _records_by_path(records: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(record["declared"]): record for record in records}


# ── the estimate ────────────────────────────────────────────────────────────


def _census(node: crew.TaskNode, repo: Path, root: Path) -> int:
    """The figure dispatch and the fit verdict charge with, on a cold cache."""
    return routing.context_census(node, repo, authority=_authority(repo), root=root)


def test_the_same_brief_estimates_lower_with_the_anchor(tmp_path: Path) -> None:
    """The same path-naming brief costs a reference, or nothing when anchored."""
    _seed_large_file(tmp_path)
    anchored = _node(write_paths=[], anchor_paths=[LARGE_PATH])
    unanchored = _node(write_paths=[], anchor_paths=[])

    anchored_tokens = _census(anchored, tmp_path, tmp_path / "cold-anchored")
    unanchored_tokens = _census(unanchored, tmp_path, tmp_path / "cold-unanchored")

    # Exactly the same brief, so the only difference is the opt-out, which is
    # worth exactly one reference charge.
    assert anchored.done_when == unanchored.done_when
    assert unanchored_tokens - anchored_tokens == _REFERENCE_COST_TOKENS
    assert anchored_tokens < unanchored_tokens


def test_an_anchored_record_keeps_its_real_bytes(tmp_path: Path) -> None:
    """The census records what it withheld, so the opt-out is visible."""
    _seed_large_file(tmp_path)
    anchored = _node(write_paths=[], anchor_paths=[LARGE_PATH])
    unanchored = _node(write_paths=[], anchor_paths=[])

    _tokens, anchored_inputs = _context_file_inputs(
        tmp_path, anchored, _authority(tmp_path)
    )
    _tokens, unanchored_inputs = _context_file_inputs(
        tmp_path, unanchored, _authority(tmp_path)
    )

    record = _records_by_path(anchored_inputs["named_files"])[LARGE_PATH]
    assert record["provenance"] == "anchor"
    assert record["counted"] is False
    assert record["estimated_tokens"] == 0
    # The real size is recorded beside the zero charge, so the opt-out is
    # visible rather than reading as a file that happens to be empty.
    assert record["bytes"] == LARGE_BYTES

    unanchored_record = _records_by_path(unanchored_inputs["named_files"])[LARGE_PATH]
    assert unanchored_record["provenance"] == "reference"
    assert unanchored_record["counted"] is True


def test_the_anchor_selects_its_own_census_entry(tmp_path: Path) -> None:
    """A cached census is not served for a differently anchored brief.

    The anchor is a charge-selecting field like the regenerated set: two nodes
    whose briefs differ only there charge the same file differently. A census
    cache keyed without it serves the unanchored figure for the anchored brief
    whenever the file stamps have not moved, which is exactly the sequence a
    coordinator runs when it previews a brief and re-dispatches it anchored.
    """
    _seed_large_file(tmp_path)
    anchored = _node(write_paths=[], anchor_paths=[LARGE_PATH])
    unanchored = _node(write_paths=[], anchor_paths=[])

    # Two caches that never met: the figures each brief produces on its own.
    cold_anchored = routing.context_census(
        anchored, tmp_path, authority=_authority(tmp_path), root=tmp_path / "cold-a"
    )
    cold_unanchored = routing.context_census(
        unanchored, tmp_path, authority=_authority(tmp_path), root=tmp_path / "cold-u"
    )
    assert cold_unanchored - cold_anchored == _REFERENCE_COST_TOKENS

    # One shared cache, the unanchored brief first: the anchored brief must
    # still be charged its own way.
    shared = tmp_path / "shared"
    shared_unanchored = routing.context_census(
        unanchored, tmp_path, authority=_authority(tmp_path), root=shared
    )
    shared_anchored = routing.context_census(
        anchored, tmp_path, authority=_authority(tmp_path), root=shared
    )

    assert shared_unanchored == cold_unanchored
    assert shared_anchored == cold_anchored
    assert shared_unanchored - shared_anchored == _REFERENCE_COST_TOKENS


def test_an_anchor_withholds_a_declared_input_charge(tmp_path: Path) -> None:
    """Anchoring the path wins over a declaration written in another spelling."""
    _seed_large_file(tmp_path)
    done_when = (
        f"the record {LARGE_PATH} is a declared input to this node; "
        "pytest reports the census charged the anchored path nothing"
    )
    anchored = _node(
        write_paths=[], anchor_paths=[f"./{LARGE_PATH}"], done_when=done_when
    )
    unanchored = _node(write_paths=[], anchor_paths=[], done_when=done_when)

    anchored_tokens = _census(anchored, tmp_path, tmp_path / "declared-anchored")
    unanchored_tokens = _census(unanchored, tmp_path, tmp_path / "declared-plain")

    # The unanchored brief pays for the declared input at its real size; the
    # anchor withholds that charge even though it is spelled differently.
    assert anchored_tokens < unanchored_tokens
    assert unanchored_tokens - anchored_tokens == math.ceil(LARGE_BYTES / 3.5)

    _tokens, inputs = _context_file_inputs(tmp_path, anchored, _authority(tmp_path))
    record = _records_by_path(inputs["named_files"])[f"./{LARGE_PATH}"]
    assert record["provenance"] == "anchor"
    assert record["bytes"] == LARGE_BYTES


# ── the refusal ─────────────────────────────────────────────────────────────


def test_a_path_both_anchored_and_written_is_refused() -> None:
    """The contradiction names the path rather than resolving one side."""
    node = _node(write_paths=[LARGE_PATH], anchor_paths=[LARGE_PATH])

    verdict = validate_node(node)

    assert verdict.ok is False
    details = " ".join(finding["detail"] for finding in verdict.findings)
    assert LARGE_PATH in details
    assert "anchor" in details
    assert "write path" in details


def test_a_regenerated_path_cannot_also_be_an_anchor() -> None:
    """A regenerated path is a write path, so the same contradiction is refused."""
    node = crew.TaskNode(
        id=PLAN,
        goal="name an anchor without loading it",
        plan=PLAN,
        section="s1",
        role="implement",
        spec_level="exact",
        done_when=DONE_WHEN,
        write_paths=[],
        regenerate_paths=[LARGE_PATH],
        anchor_paths=[LARGE_PATH],
        time_budget="20m",
    )

    verdict = validate_node(node)

    assert verdict.ok is False
    assert LARGE_PATH in " ".join(finding["detail"] for finding in verdict.findings)


# ── the run record ──────────────────────────────────────────────────────────


def _snapshot(path: Path) -> tuple[str, ...]:
    if not path.is_dir():
        return ()
    return tuple(sorted(item.name for item in path.iterdir()))


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    real_live = crew.live_dir()
    before = _snapshot(real_live)
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    yield config_home
    assert _snapshot(real_live) == before


@pytest.fixture()
def repo(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / "skills" / "reckon-build" / "scripts").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (root / "skills" / "reckon-build" / "scripts" / "worktree_fleet.py").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (root / "docs" / "plans" / "plan-a.html").write_text(
        '<meta name="docs-project" content="proj">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="plan-a">'
        '<h2 id="s1">s1</h2>',
        encoding="utf-8",
    )
    _seed_large_file(root)
    (root / "docs" / "state" / PROJECT).mkdir(parents=True, exist_ok=True)
    ledger.write(PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=root)
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "docs", "skills"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    (home / "mounts.json").write_text(
        json.dumps({"proj": str(root / "docs")}), encoding="utf-8"
    )
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *a, **k: CONFIG
    )
    return root


def _dispatch_arguments(
    repo: Path,
    node_id: str,
    *,
    anchors: tuple[str, ...] = (),
    write_paths: tuple[str, ...] = (),
) -> list[str]:
    arguments = [
        "crew",
        "dispatch",
        "--project",
        PROJECT,
        "--plan",
        PLAN,
        "--section",
        "s1",
        "--spec-level",
        "exact",
        "--node",
        node_id,
        "--goal",
        "name an anchor without loading it",
        "--done-when",
        DONE_WHEN,
        "--session",
        "dry-run-session",
        "--repo",
        str(repo),
        "--dry-run",
    ]
    for path in write_paths:
        arguments += ["--write-path", path]
    for path in anchors:
        arguments += ["--anchor-path", path]
    return arguments


def test_the_anchor_is_recorded_on_the_runs_node_definition(
    home: Path, repo: Path
) -> None:
    """The dry run builds the same node a launch does, and it carries the anchor."""
    result = CliRunner().invoke(
        cli_module.main,
        _dispatch_arguments(
            repo,
            "node-anchor",
            anchors=(LARGE_PATH,),
            write_paths=(WRITE_PATH,),
        ),
    )
    payload = json.loads(result.output)

    assert result.exit_code == 0, result.output
    assert payload["node"]["anchor_paths"] == [LARGE_PATH]
    # The anchor is not a write path: it is not claimed against a peer.
    assert LARGE_PATH not in payload["node"]["write_paths"]


def test_dispatch_refuses_a_path_that_is_both_anchor_and_write_path(
    home: Path, repo: Path
) -> None:
    """The refusal comes from the dispatch surface and names the path."""
    result = CliRunner().invoke(
        cli_module.main,
        _dispatch_arguments(
            repo, "node-contradiction", anchors=(LARGE_PATH,), write_paths=(LARGE_PATH,)
        ),
    )
    payload = json.loads(result.output)

    assert result.exit_code != 0, result.output
    assert payload["ok"] is False
    combined = json.dumps(payload)
    assert LARGE_PATH in combined
    assert "anchor" in combined


# ── the queued launch ───────────────────────────────────────────────────────


def test_a_queued_request_keeps_its_anchor_through_reconstruction() -> None:
    """A queued request's node is rebuilt from the pointer; the anchor survives.

    The queued launch never carries the built ``TaskNode``: it stores the node
    definition and rebuilds one at admission, so a field the rebuild does not
    read is a field the launched worker never sees.
    """
    from reckon.crew.queue_admission import _node_from_pointer

    pointer = {
        "run_id": "r-20261105T000000000000-queued-anchor",
        "node": {
            "id": "node-queued",
            "goal": "name an anchor without loading it",
            "plan": PLAN,
            "section": "s1",
            "role": "implement",
            "spec_level": "exact",
            "done_when": DONE_WHEN,
            "write_paths": ["reckon/other.py"],
            "anchor_paths": [LARGE_PATH],
            "time_budget": "20m",
        },
    }

    node = _node_from_pointer(pointer)

    assert node.anchor_paths == [LARGE_PATH]
    assert node.as_dict()["anchor_paths"] == [LARGE_PATH]


def test_a_run_record_rebuild_keeps_its_anchor(tmp_path: Path) -> None:
    """A node rebuilt from a live run record is charged as its record says.

    The record rebuild names each field it keeps, so a field it omits is one
    the re-estimate silently charges again — the anchor would be billed at the
    reference cost, and a regenerated path would not read as written.
    """
    from reckon.crew.dispatch_sessions import _recorded_task_node

    _seed_large_file(tmp_path)
    original = crew.TaskNode(
        id="node-recorded",
        goal="name an anchor without loading it",
        plan=PLAN,
        section="s1",
        role="implement",
        spec_level="exact",
        done_when=DONE_WHEN,
        write_paths=[],
        regenerate_paths=["docs/research/rewritten_record.html"],
        anchor_paths=[LARGE_PATH],
        time_budget="20m",
    )
    record = {"run_id": "r-20261105T000000000000-recorded", "node": original.as_dict()}

    rebuilt = _recorded_task_node(record)

    assert rebuilt.anchor_paths == [LARGE_PATH]
    assert rebuilt.regenerate_paths == ["docs/research/rewritten_record.html"]
    assert rebuilt.write_paths == original.write_paths
    # The rebuilt node estimates as the record's own node does: the anchor is
    # withheld from the charge rather than re-billed as a reference.
    assert _census(rebuilt, tmp_path, tmp_path / "recorded") == _census(
        original, tmp_path, tmp_path / "recorded-original"
    )
