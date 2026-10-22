"""A finding with a line but no file names its fallback, not a bare line.

The repair composer renders a finding's place as ``file`` or ``file:line``, and
the review-waits-for-its-repair refusal borrows that rendering so the refusal and
the brief name one location the same way. A finding the reviewer stated with a
line and no file used to yield ``:12`` — a string no reader can open, and truthy,
so the refusal's fallback to the review's findings never applied and a reader was
sent to a file that was never named. The composer now returns nothing for a
finding that names no file, so every caller's fallback applies and no caller had
to change.

Every dispatch case drives the real dispatch against a real git history and a
real stored review, so the refusal is exercised rather than stubbed.
"""

from __future__ import annotations

import importlib
import os
import subprocess
from pathlib import Path

import pytest

from reckon.crew import repair as repair_module
from reckon.crew import review as review_module
from reckon.crew import runs

dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = "line-only-finding-names-its-fallback"
SESSION = "coordinator-fixture"

LOCAL_BACKEND = "clive"

# The severity a finding states to block the node's landing, taken from the
# review parser's own vocabulary rather than invented here.
BLOCKING = review_module.BLOCKING_FINDING_SEVERITY

FINDING = "the fit ignores the converter's behaviour at DC, so the model is wrong"

# The reference a reviewer writes when the finding states a line and no file:
# the parser splits on the last colon, so the file side is left empty.
LINE_ONLY_REF = ":12"


def _backend(command: str) -> dict:
    return {
        "launch": "cli",
        "command": command,
        "model": "some-model",
        "effort": "high",
        "sandbox": "worktree-full",
        "session_reuse": True,
        "time_budget": "25m",
    }


CONFIG = {
    "default_backend": LOCAL_BACKEND,
    "local_backend": LOCAL_BACKEND,
    "backends": {LOCAL_BACKEND: _backend("codex")},
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _commit(repository: Path, paths: dict[str, str], message: str) -> str:
    for name, body in paths.items():
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    subprocess.run(
        ["git", "add", *paths], cwd=repository, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A repository, an isolated crew home and a reviewed implement run."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repo = tmp_path / "repo"
    repo.mkdir()
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(repo, *arguments)
    _commit(repo, {"seed.txt": "seed\n"}, "chore: seed")
    reviewed = _commit(
        repo,
        {
            "pkg/mod.py": "VALUE = 1\n",
            "docs/plans/fixture-plan.html": (
                '<meta name="docs-project" content="' + PROJECT + '">'
                '<meta name="reckon-type" content="plan">'
                '<meta name="plan-slug" content="fixture-plan">'
                '<h2 id="s2">A line-only finding names its fallback</h2>'
            ),
        },
        "feat: the reviewed change",
    )
    (config_home / "mounts.json").write_text(
        '{"' + PROJECT + '": "' + str(repo / "docs") + '"}'
    )
    monkeypatch.setattr(
        review_module,
        "review_store_root",
        lambda base_dir=None: config_home / "reviews",
    )
    return {
        "tmp": tmp_path,
        "config_home": config_home,
        "repo": repo,
        "reviewed": reviewed,
    }


def _write_run(world: dict, *, run_id: str, head: str) -> dict:
    """One complete, unpromoted implement run whose worktree is the repo."""
    repo = world["repo"]
    manifest = world["config_home"] / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: [{head}]\n", encoding="utf-8"
    )
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repo),
        "worktree": str(repo),
        "process_alive": False,
        "launch": "in-harness",
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {
            "id": run_id,
            "plan": "fixture-plan",
            "write_paths": ["pkg/mod.py"],
        },
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _store_review(run_id: str, *, base: str, head: str, finding: str | None) -> dict:
    """Store a review of ``run_id`` at the revision it read.

    ``finding`` is written in the parser's own emitted grammar, so the stored
    severity is the one a reviewer would have written rather than a field this
    test invented, and the reference is the line-only shape under test.
    """
    emitted = [
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    ]
    if finding is not None:
        emitted.append(f"FINDING {LINE_ONLY_REF} {BLOCKING}: {finding}")
    return review_module.store_review(
        {
            **review_module.parse_review("\n".join(emitted)),
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
        }
    )


def _resumed_run(world: dict, run_id: str, *, finding: str | None) -> dict:
    """A run reviewed at one head and resumed onto a new source head."""
    reviewed = world["reviewed"]
    _write_run(world, run_id=run_id, head=reviewed)
    _store_review(run_id, base=reviewed, head=reviewed, finding=finding)
    _commit(world["repo"], {"pkg/mod.py": "VALUE = 2\n"}, "fix: a source move")
    return runs.read_pointer(run_id)


def world_worktrees(repo: Path) -> Path:
    """The worktree directory the fixture's repository shares a parent with."""
    return repo.parent / "worktrees"


def _drive(record: dict, monkeypatch: pytest.MonkeyPatch, captured: dict) -> dict:
    """Run the real dispatch for one run, capturing any worktree it asked for."""
    repo = Path(record["repo"])
    worktrees = world_worktrees(repo)

    def prepare(_repo, session, worker, base):
        path = worktrees / str(session) / str(worker)
        path.mkdir(parents=True, exist_ok=True)
        captured["worker"] = worker
        captured["path"] = str(path)
        return {
            "path": str(path),
            "base": base,
            "base_sha": _git(repo, "rev-parse", "HEAD"),
        }

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare)
    from reckon import crew
    from reckon.crew import recovery
    from reckon.crew.recovery_review_dispatch import dispatch_review_for_run

    try:
        with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
            return dispatch_review_for_run(
                record, config=CONFIG, launcher=lambda *a, **k: os.getpid()
            )
    finally:
        if crew.watch_state(PROJECT)["watcher_live"]:
            recovery.unwatch(PROJECT)


def test_a_line_only_finding_has_no_location() -> None:
    """A finding stating a line and no file returns the empty location."""
    line_only = {"file": "", "line": "12", "text": "..."}
    assert repair_module._finding_location(line_only) == "", (
        repair_module._finding_location(line_only)
    )
    assert repair_module._finding_location({"line": "12"}) == ""
    assert repair_module._finding_location({"file": "", "line": "", "text": "x"}) == ""


def test_the_parser_states_a_line_only_finding_with_no_file() -> None:
    """The bare-line reference parses to a finding this renderer must fall back on.

    The empty location is a fact about a shape the review grammar really emits,
    not one this test constructed: the reference splits on its last colon and
    leaves the file side empty.
    """
    parsed = review_module.parse_review(
        "\n".join(
            [f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS]
            + [f"FINDING {LINE_ONLY_REF} {BLOCKING}: {FINDING}"]
        )
    )
    assert len(parsed["findings"]) == 1, parsed["findings"]
    finding = parsed["findings"][0]
    assert finding["file"] == "", finding
    assert finding["line"] == "12", finding
    assert repair_module._finding_location(finding) == "", (
        repair_module._finding_location(finding)
    )


def test_a_finding_with_file_and_line_still_names_its_place() -> None:
    """A finding naming both renders ``file:line``, and a file alone renders file."""
    both = {"file": "pkg/mod.py", "line": "1"}
    file_only = {"file": "pkg/mod.py", "line": ""}
    assert repair_module._finding_location(both) == "pkg/mod.py:1", (
        repair_module._finding_location(both)
    )
    assert repair_module._finding_location(file_only) == "pkg/mod.py", (
        repair_module._finding_location(file_only)
    )


def test_the_refusal_for_a_line_only_finding_names_the_reviews_findings(
    world: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal falls back rather than printing a bare line number.

    The guarded thing is made to happen: a real dispatch over a real stored
    review whose only blocking finding carries a line and no file is refused,
    and the refusal renders the review's findings instead of ``:12``.
    """
    record = _resumed_run(world, "r-line-only", finding=FINDING)
    captured: dict = {}

    report = _drive(record, monkeypatch, captured)

    assert report["dispatched"] is False, report
    assert report.get("refused") is True, report
    reason = report["reason"]
    assert "the review's findings" in reason, reason
    assert LINE_ONLY_REF not in reason, reason
    # The refusal is still the same refusal: the finding's dispose command is
    # named, so the coordinator can lift it as before.
    assert "--finding 0" in reason, reason
    assert "crew dispose" in reason, reason
    assert captured == {}, captured
