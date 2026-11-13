"""Merge uses the run promotion verdict before moving or pushing a head.

Set RECKON_TEST_SKIP_MERGE_PROMOTION_CHECK=1 for the negative control: the
refused head then lands and the refusal assertion fails.
"""

import json
import os

from click.testing import CliRunner

from reckon.cli import main
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git
from tests.helpers.landing_repository import _write_origin_and_checkout


def test_merge_refuses_unpromotable_head(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    origin, repository = _write_origin_and_checkout(tmp_path)
    (home / "mounts.json").write_text(json.dumps({"proj": str(repository / "docs")}))
    worktree = tmp_path / "worktree"
    git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    (worktree / "change.txt").write_text("introduced by run\n")
    git(worktree, "add", "change.txt")
    git(
        worktree,
        "commit",
        "-q",
        "-m",
        "fix: record a change",
        "-m",
        "Give the merge a visible marker.",
    )
    head = git(worktree, "rev-parse", "HEAD")
    run_id = "r-20261105T120000000000-merge-refusal"
    baseline_log = tmp_path / "baseline.log"
    baseline_log.write_text(
        "FAILED tests/test_example.py::test_existing - AssertionError\n1 failed\nEXIT=1\n"
    )
    head_log = tmp_path / "head.log"
    head_log.write_text(
        "FAILED tests/test_example.py::test_added - AssertionError\n1 failed\nEXIT=1\n"
    )
    baseline = {
        "revision": git(repository, "rev-parse", "HEAD"),
        "command": "true",
        "exit_status": 1,
        "log_path": str(baseline_log),
        "completed": True,
        "failure_count": 1,
        "failure_ids": ["tests/test_example.py::test_existing"],
    }
    after = {
        "revision": head,
        "command": "true",
        "exit_status": 1,
        "log_path": str(head_log),
        "completed": True,
        "failure_count": 1,
        "failure_ids": ["tests/test_example.py::test_added"],
    }
    manifest = tmp_path / "manifest.md"
    manifest.write_text(
        "node: merge-refusal\nstatus: complete\ncommits: "
        + head
        + "\n"
        + "baseline_suite: "
        + json.dumps(baseline)
        + "\nafter_suite: "
        + json.dumps(after)
        + "\ntests: true\ntest_logs: "
        + str(head_log)
        + "\n"
    )
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "proj",
            "repo": str(repository),
            "worktree": str(worktree),
            "base_sha": git(repository, "rev-parse", "HEAD"),
            "launch": "in-harness",
            "role": "implement",
            "created_at": "2026-11-05T12:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": "merge-refusal",
                "plan": "plan-a",
                "section": "merge-guard",
                "write_paths": ["change.txt"],
            },
        },
    )
    before_pointer = pointer_path(run_id).read_bytes()
    before_main = git(repository, "rev-parse", "HEAD")
    before_plan = (repository / "docs" / "plans" / "plan-a.html").read_bytes()
    dry = CliRunner().invoke(
        main,
        [
            "crew",
            "complete",
            "--dry-run",
            "--run",
            run_id,
            "--gate",
            "passed",
            "--commit",
            head,
            "--checkout-path",
            str(repository),
            "--gate-command",
            "true",
            "--gate-exit-status",
            "1",
            "--gate-log-path",
            str(head_log),
        ],
    )
    assert dry.exit_code != 0, dry.output
    dry_report = json.loads(dry.output)
    assert dry_report["dry_run"] is True
    assert any(
        "red base" in refusal or "baseline" in refusal or "exit status 1" in refusal
        for refusal in dry_report["refusals"]
    ), dry_report
    assert pointer_path(run_id).read_bytes() == before_pointer
    assert git(repository, "rev-parse", "HEAD") == before_main
    assert (repository / "docs" / "plans" / "plan-a.html").read_bytes() == before_plan
    assert not (repository / "docs" / "state" / "proj" / "crew.json").exists()
    if os.environ.get("RECKON_TEST_SKIP_MERGE_PROMOTION_CHECK"):
        from reckon.crew import merge as merge_module

        monkeypatch.setattr(
            merge_module, "require_promotable_head", lambda *args, **kwargs: None
        )
    merge = CliRunner().invoke(
        main,
        [
            "crew",
            "merge",
            "--project",
            "proj",
            "--head",
            head,
            "--message",
            "fix: land the change",
            "--body",
            "Carry the reviewed change onto main.",
        ],
    )
    assert merge.exit_code != 0, merge.output
    assert (
        "exit status 1" in merge.output
        or "red base" in merge.output
        or "baseline" in merge.output
    ), merge.output
    assert git(repository, "rev-parse", "HEAD") == before_main
    assert git(origin, "rev-parse", "refs/heads/main") == before_main


def test_merge_lands_promotable_head(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    origin, repository = _write_origin_and_checkout(tmp_path)
    (home / "mounts.json").write_text(json.dumps({"proj": str(repository / "docs")}))
    worktree = tmp_path / "worktree"
    git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    (worktree / "change.txt").write_text("introduced by run\n")
    git(worktree, "add", "change.txt")
    git(
        worktree,
        "commit",
        "-q",
        "-m",
        "fix: record a change",
        "-m",
        "Give the merge a visible marker.",
    )
    head = git(worktree, "rev-parse", "HEAD")
    run_id = "r-20261105T120100000000-merge-accepted"
    gate_log = tmp_path / "gate.log"
    gate_log.write_text("# command: true\nEXIT=0\n")
    manifest = tmp_path / "manifest.md"
    manifest.write_text(
        "node: merge-accepted\nstatus: complete\ncommits: "
        + head
        + "\ntests: true\ntest_logs: "
        + str(gate_log)
        + "\n"
    )
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "proj",
            "repo": str(repository),
            "worktree": str(worktree),
            "base_sha": git(repository, "rev-parse", "HEAD"),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-11-05T12:01:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": "merge-accepted",
                "plan": "plan-a",
                "section": "merge-guard",
                "write_paths": ["change.txt"],
            },
        },
    )
    dry_args = [
        "--run",
        run_id,
        "--gate",
        "passed",
        "--commit",
        head,
        "--checkout-path",
        str(repository),
        "--no-impl-change",
        "the fixture moves no plan",
        "--gate-command",
        "true",
        "--gate-exit-status",
        "0",
        "--gate-log-path",
        str(gate_log),
    ]
    dry = CliRunner().invoke(main, ["crew", "complete", "--dry-run", *dry_args])
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.output)["would_promote"] is True
    merged = CliRunner().invoke(
        main,
        [
            "crew",
            "merge",
            "--project",
            "proj",
            "--head",
            head,
            "--run",
            run_id,
            "--commit",
            head,
            "--no-impl-change",
            "the fixture moves no plan",
            "--gate-command",
            "true",
            "--gate-exit-status",
            "0",
            "--gate-log-path",
            str(gate_log),
            "--message",
            "fix: land the change",
            "--body",
            "Carry the reviewed change onto main.",
        ],
    )
    assert merged.exit_code == 0, merged.output
    assert "introduced by run" in git(repository, "show", "HEAD:change.txt")
    assert git(origin, "rev-parse", "refs/heads/main") == git(
        repository, "rev-parse", "HEAD"
    )


def test_merge_refusal_names_the_check_for_a_named_run(tmp_path, monkeypatch):
    """A merge told which run it lands consults that run's promotion verdict.

    The run is named rather than inferred from the heads, so the guard reads the
    pointer the coordinator named and reports the check that refused it.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    origin, repository = _write_origin_and_checkout(tmp_path)
    (home / "mounts.json").write_text(json.dumps({"proj": str(repository / "docs")}))
    worktree = tmp_path / "worktree"
    git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    (worktree / "change.txt").write_text("introduced by run\n")
    git(worktree, "add", "change.txt")
    git(
        worktree,
        "commit",
        "-q",
        "-m",
        "fix: record a change",
        "-m",
        "Give the merge a visible marker.",
    )
    head = git(worktree, "rev-parse", "HEAD")
    run_id = "r-20261113T050000000000-merge-named-run"
    gate_log = tmp_path / "gate.log"
    gate_log.write_text("EXIT=1\n")
    manifest = tmp_path / "manifest.md"
    manifest.write_text(
        "node: merge-named-run\nstatus: complete\ncommits: "
        + head
        + "\ntests: true\nexit=1\ntest_logs: "
        + str(gate_log)
        + "\n"
    )
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "proj",
            "repo": str(repository),
            "worktree": str(worktree),
            "base_sha": git(repository, "rev-parse", "HEAD"),
            "launch": "in-harness",
            "role": "implement",
            "created_at": "2026-11-13T05:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": "merge-named-run",
                "plan": "plan-a",
                "section": "merge-guard",
                "write_paths": ["change.txt"],
            },
        },
    )
    before_main = git(repository, "rev-parse", "HEAD")
    merge = CliRunner().invoke(
        main,
        [
            "crew",
            "merge",
            "--project",
            "proj",
            "--head",
            head,
            "--run",
            run_id,
            "--commit",
            head,
            "--gate-command",
            "true",
            "--gate-exit-status",
            "1",
            "--gate-log-path",
            str(gate_log),
            "--message",
            "fix: land the change",
            "--body",
            "Carry the reviewed change onto main.",
        ],
    )
    assert merge.exit_code != 0, merge.output
    assert "promotion dry run refused run " + run_id in merge.output, merge.output
    assert "exit status 1" in merge.output, merge.output
    assert git(repository, "rev-parse", "HEAD") == before_main
    assert git(origin, "rev-parse", "refs/heads/main") == before_main
