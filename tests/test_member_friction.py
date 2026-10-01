"""A named member resolves from the registered checkout, and unnamed runs
never share a session identity.

Two frictions measured on the dispatch surface. A dispatch that names a roster
member and a lane resolves the project's registered repository when --repo is
omitted, exactly as a dispatch that names no member does, and refuses with a
message naming --repo only when the project has no registered checkout at all.
A dispatch that names no member carries its own per-run identity, so a live
review worker from an earlier run never holds a later dispatch in flight.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import recovery
from reckon.crew.node import member_in_flight_verdict

CONFIG = {
    "default_backend": "worker",
    "local_backend": "worker",
    "backends": {
        "worker": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _real_stores() -> tuple[list[str], dict[str, bytes]]:
    """Name the real pointer directory and every real roster right now.

    A dispatcher that wrote outside the temporary configuration home would
    leave a live pointer or a roster row here, so comparing this before and
    after is a write-side check rather than a read of code that never ran.
    """
    live = Path.home() / ".config" / "reckon" / "crew" / "live"
    pointers = sorted(entry.name for entry in live.iterdir()) if live.is_dir() else []
    checkout = Path(__file__).resolve().parents[1]
    rosters = {
        str(path): path.read_bytes()
        for path in sorted(checkout.glob("docs/state/*/crew.json"))
    }
    return pointers, rosters


@pytest.fixture()
def isolated_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    before = _real_stores()

    repo = tmp_path_repo = tmp_path / "repo"
    (repo / "docs" / "plans").mkdir(parents=True)
    (repo / "docs" / "plans" / "fixture.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="reviews">Independent review dispatch</h2>',
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({"sample": str(repo / "docs")}), encoding="utf-8"
    )
    ledger.register_member("sample", "fixture-member", harness="worker", root=repo)

    yield config_home, tmp_path_repo

    assert _real_stores() == before


def _member_node() -> crew.TaskNode:
    return crew.TaskNode(
        id="member-resolution",
        goal="resolve a named member from the project's registered checkout",
        plan="fixture",
        section="reviews",
        role="implement",
        spec_level="exact",
        done_when=(
            "pytest tests/test_member_friction.py reports the registered-checkout "
            "case passing"
        ),
        write_paths=["records/member-resolution.json"],
        time_budget="20m",
    )


def test_member_dispatch_from_the_registered_checkout_resolves_without_repo(
    isolated_project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lane-naming dispatch inside the checkout resolves the mount itself.

    This is the validating path the CLI dry run takes: it hands the raw
    ``--repo`` through, so an omitted flag arrives as ``None`` while the
    launching path would have resolved the project's registered mount.
    """
    _config_home, repo = isolated_project
    monkeypatch.chdir(repo)

    resolution = crew.plan_dispatch(
        node=_member_node(),
        config=CONFIG,
        project="sample",
        repo=None,
        local=True,
        member="fixture-member",
    )

    assert resolution.validation.ok, resolution.validation.findings
    assert resolution.authority is not None
    assert resolution.authority["write"]["repository"] == str(repo.resolve())
    assert resolution.backend == "worker"


def test_member_dispatch_without_a_registered_checkout_names_repo(
    isolated_project: tuple[Path, Path],
) -> None:
    """No registered checkout is the one case that still refuses, naming the
    flag that supplies the missing repository."""
    _config_home, _repo = isolated_project

    with pytest.raises(crew.CrewError) as refusal:
        crew.plan_dispatch(
            node=_member_node(),
            config=CONFIG,
            project="unmounted",
            repo=None,
            local=True,
            member="fixture-member",
        )
    assert "--repo" in str(refusal.value)


def _scoring_pointer(config_home: Path, repo: Path, run_id: str) -> dict:
    manifest = config_home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: {run_id}\n",
        encoding="utf-8",
    )
    record = {
        "run_id": run_id,
        "project": "sample",
        "repo": str(repo),
        "node": {"id": run_id, "plan": "fixture", "section": "reviews"},
        "backend": "worker",
        "launch": "cli",
        "argv": ["codex"],
        "phase": "complete",
        "process_alive": False,
        "session": "session-orchestrating",
        "manifest_path": str(manifest),
    }
    crew._write_json(crew.pointer_path(run_id), record)
    return record


def test_unnamed_dispatch_is_admitted_while_a_review_worker_is_alive(
    isolated_project: tuple[Path, Path],
) -> None:
    """The review worker from the first run is still alive when the second
    dispatch runs, yet the second is admitted: an unnamed dispatch carries its
    own identity rather than the dispatching session's shared one."""
    config_home, repo = isolated_project
    first_source = _scoring_pointer(config_home, repo, "r-first")
    second_source = _scoring_pointer(config_home, repo, "r-second")
    launched: list[dict] = []

    def launcher(*args, **kwargs):
        launched.append(kwargs)
        return os.getpid()

    first = recovery.dispatch_review_for_run(
        first_source, config=CONFIG, launcher=launcher
    )
    assert first["dispatched"] is True, first.get("reason")
    first_pointer = crew.read_pointer(first["review_run_id"])
    assert first_pointer["pid"] == os.getpid()
    assert member_in_flight_verdict(first_pointer).blocks is True
    assert first_pointer["member"] == f"disposable-{first['review_run_id']}"

    second = recovery.dispatch_review_for_run(
        second_source, config=CONFIG, launcher=launcher
    )
    assert second["dispatched"] is True, second.get("reason")

    second_pointer = crew.read_pointer(second["review_run_id"])
    assert second_pointer["member"] == f"disposable-{second['review_run_id']}"
    assert second_pointer["member"] != first_pointer["member"]
    assert len(launched) == 2
