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
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew, crew_dispatch_commands, ledger
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


def test_the_same_brief_estimates_lower_with_the_anchor(tmp_path: Path) -> None:
    """The same path-naming brief costs a reference, or nothing when anchored."""
    _seed_large_file(tmp_path)
    anchored = _node(write_paths=[], anchor_paths=[LARGE_PATH])
    unanchored = _node(write_paths=[], anchor_paths=[])

    anchored_tokens, anchored_inputs = _context_file_inputs(
        tmp_path, anchored, _authority(tmp_path)
    )
    unanchored_tokens, unanchored_inputs = _context_file_inputs(
        tmp_path, unanchored, _authority(tmp_path)
    )

    # Exactly the same brief, so the only difference is the opt-out.
    assert anchored.done_when == unanchored.done_when
    assert unanchored_tokens == _REFERENCE_COST_TOKENS
    assert anchored_tokens == 0
    assert anchored_tokens < unanchored_tokens

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


def test_an_anchor_withholds_a_declared_input_charge(tmp_path: Path) -> None:
    """Anchoring the path wins over a declaration written in another spelling."""
    _seed_large_file(tmp_path)
    node = _node(
        write_paths=[],
        anchor_paths=[f"./{LARGE_PATH}"],
        done_when=(
            f"the record {LARGE_PATH} is a declared input to this node; "
            "pytest reports the census charged the anchored path nothing"
        ),
    )

    tokens, inputs = _context_file_inputs(tmp_path, node, _authority(tmp_path))

    assert tokens == 0
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
