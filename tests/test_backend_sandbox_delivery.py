"""The read-only tier grants its declared write roots and never the worktree.

The Claude dialect draws the read-only boundary as a grant rather than a
permission mode: a mode such as ``plan`` withheld every write, so a node could
not deliver its declared files yet still reported a completed turn. These tests
assert only the argv a dialect builds — no live model is invoked, and the grant
is checked by reading the rendered invocation.
"""

from __future__ import annotations

from pathlib import Path

from reckon import _backends

CODEX = {"launch": "cli", "command": "codex", "sandbox": "worktree-full"}
CLAUDE = {"launch": "cli", "command": "claude", "sandbox": "worktree-full"}


def _read_only(backend: dict) -> dict:
    return dict(backend, sandbox="read-only")


def _computed_write_roots(
    *,
    repository: Path,
    run_directory: Path,
    reports_directory: Path,
    manifest_path: Path,
) -> list[str]:
    """Resolve the roots sandbox_write_roots computes for the read-only tier."""
    roots = _backends.sandbox_write_roots(
        _read_only(CLAUDE),
        repository=str(repository),
        run_directory=str(run_directory),
        reports_directory=str(reports_directory),
        manifest_path=str(manifest_path),
    )
    assert roots is not None
    return [str(root) for root in roots]


def test_claude_read_only_argv_has_no_write_withholding_permission_mode(
    tmp_path: Path,
) -> None:
    """The read-only tier must not resolve back to a mode that withholds writes.

    This is the guard that fails if the tier ever regresses to `--permission-mode
    plan`: a denied deliverable is worse than a refused dispatch, because the run
    looks successful from every angle except the missing file.
    """
    plan = _backends.launch_plan(
        backend_name="b",
        backend=_read_only(CLAUDE),
        prompt="p",
        worktree=str(tmp_path / "worktree"),
        manifest_path=str(tmp_path / "delivery" / "manifest.md"),
        writable_directories=[str(tmp_path / "run"), str(tmp_path / "reports")],
    )
    assert "--dangerously-skip-permissions" in plan.argv
    assert "--permission-mode" not in plan.argv
    assert "plan" not in plan.argv


def test_claude_read_only_grants_every_computed_write_root(tmp_path: Path) -> None:
    """Each root sandbox_write_roots computes appears as a writable directory."""
    worktree = tmp_path / "worktree"
    run_dir = tmp_path / "run"
    reports = tmp_path / "reports"
    worktree.mkdir()
    run_dir.mkdir()
    reports.mkdir()
    manifest = tmp_path / "run" / "manifest.md"
    roots = _computed_write_roots(
        repository=worktree,
        run_directory=run_dir,
        reports_directory=reports,
        manifest_path=manifest,
    )
    plan = _backends.launch_plan(
        backend_name="b",
        backend=_read_only(CLAUDE),
        prompt="p",
        worktree=str(worktree),
        manifest_path=str(manifest),
        writable_directories=roots,
    )
    grant = plan.argv[plan.argv.index("--add-dir") + 1 :]
    for root in roots:
        assert root in grant


def test_claude_read_only_grant_excludes_the_worktree(tmp_path: Path) -> None:
    """The repository under test never enters the writable-directory grant."""
    worktree = tmp_path / "worktree"
    run_dir = tmp_path / "run"
    reports = tmp_path / "reports"
    worktree.mkdir()
    run_dir.mkdir()
    reports.mkdir()
    manifest = tmp_path / "run" / "manifest.md"
    roots = _computed_write_roots(
        repository=worktree,
        run_directory=run_dir,
        reports_directory=reports,
        manifest_path=manifest,
    )
    plan = _backends.launch_plan(
        backend_name="b",
        backend=_read_only(CLAUDE),
        prompt="p",
        worktree=str(worktree),
        manifest_path=str(manifest),
        writable_directories=roots,
    )
    grant = plan.argv[plan.argv.index("--add-dir") + 1 :]
    assert str(worktree) not in grant
    # The worktree appears nowhere else in a Claude invocation (no -C flag).
    assert str(worktree) not in plan.argv


def test_codex_read_only_argv_is_unchanged(tmp_path: Path) -> None:
    """The codex dialect's read-only argv stays byte-identical."""
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    manifest = delivery / "manifest.md"
    roots = [
        str(tmp_path / "run"),
        str(tmp_path / "reports"),
        str(tmp_path / "other"),
    ]
    plan = _backends.launch_plan(
        backend_name="b",
        backend=_read_only(CODEX),
        prompt="p",
        worktree=str(tmp_path / "worktree"),
        manifest_path=str(manifest),
        writable_directories=roots,
    )
    expected = [
        "codex",
        "exec",
        "--json",
        "-C",
        str(delivery),
        "--sandbox",
        "workspace-write",
        "--skip-git-repo-check",
    ]
    # Every root outside the delivery workspace is admitted as a writable dir,
    # in the order the caller passed them. None of the roots here lies under
    # the delivery workspace, so all three survive the reachability filter.
    for root in roots:
        expected += ["--add-dir", root]
    expected.append("-")
    assert plan.argv == expected
    assert plan.cwd == str(delivery)
