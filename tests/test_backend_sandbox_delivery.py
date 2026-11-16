"""The read-only tier grants its declared write roots and never the worktree.

The Claude dialect draws the read-only boundary as a grant rather than a
permission mode: a mode such as ``plan`` withheld every write, so a node could
not deliver its declared files yet still reported a completed turn. These tests
assert only the argv a dialect builds — no live model is invoked, and the grant
is checked by reading the rendered invocation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _backends
from reckon.hooks.install import worker_inbox_group

CODEX = {"launch": "cli", "command": "codex", "sandbox": "worktree-full"}
CLAUDE = {"launch": "cli", "command": "claude", "sandbox": "worktree-full"}


_INBOX_HOOK_KEY = "/<session-flags>/config.toml:post_tool_use:0:0"
_INBOX_HOOK_HASH = "sha256:" + "a" * 64


def _inbox_trust_answer(probe: _backends.BudgetProbe) -> dict:
    """Answer the inbox hook-trust probe with the flag it asked to trust.

    The launch reads the hook's identity from the harness itself, so the
    answer is composed from the override the launch submitted — the same
    round trip a live app server performs — and the trust value stays a fixed
    one this test can pin rather than one read from whatever codex the
    machine happens to carry.
    """
    import tomllib

    override = probe.argv[probe.argv.index("-c") + 1]
    hook = tomllib.loads(override)["hooks"]["PostToolUse"][0]["hooks"][0]
    return {
        "result": {
            "data": [
                {
                    "errors": [],
                    "hooks": [
                        {
                            "key": _INBOX_HOOK_KEY,
                            "currentHash": _INBOX_HOOK_HASH,
                            "source": "sessionFlags",
                            "eventName": "postToolUse",
                            "handlerType": "command",
                            "command": hook["command"],
                        }
                    ],
                }
            ]
        }
    }


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
    """The repository under test never enters the writable-directory grant.

    The dialect's own argv, so the fence wrapper is not composed: the fence
    binds the worktree read-only, which means its invocation names that path by
    design, and what this asserts is what the dialect is granted — the delivery
    roots sandbox_write_roots computes, and never the repository.
    """
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
        fence=False,
    )
    grant = plan.argv[plan.argv.index("--add-dir") + 1 :]
    assert str(worktree) not in grant
    # The worktree appears nowhere else in a Claude invocation (no -C flag).
    assert str(worktree) not in plan.argv


def test_codex_read_only_argv_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The codex dialect's read-only argv stays byte-identical.

    The dialect's own argv, so the fence wrapper is not composed: this pins
    what the dialect builds from the sandbox tier, while the fence is a
    wrapper around a launch and is asserted on its own. Two overrides ride
    beside that tier and are pinned with it: the table disabling undeclared
    MCP servers, resolved from a home this test owns rather than the
    operator's, and the inbox hook's session-flag trust, whose probe is
    answered here.
    """
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    (tmp_path / ".codex").mkdir()
    manifest = delivery / "manifest.md"
    roots = [
        str(tmp_path / "run"),
        str(tmp_path / "reports"),
        str(tmp_path / "other"),
    ]
    monkeypatch.setattr(_backends, "run_probe", _inbox_trust_answer)
    plan = _backends.launch_plan(
        backend_name="b",
        backend=_read_only(CODEX),
        prompt="p",
        worktree=str(tmp_path / "worktree"),
        manifest_path=str(manifest),
        writable_directories=roots,
        fence=False,
        fence_home=str(tmp_path),
    )
    hook_command = worker_inbox_group()["hooks"][0]["command"]
    expected = [
        "codex",
        "exec",
        "--json",
        "-C",
        str(delivery),
        "-c",
        "mcp_servers={}",
        "-c",
        'hooks.PostToolUse=[{hooks=[{type="command", command='
        + json.dumps(hook_command)
        + "}]}]",
        "-c",
        "hooks.state={"
        + json.dumps(_INBOX_HOOK_KEY)
        + "={trusted_hash="
        + json.dumps(_INBOX_HOOK_HASH)
        + "}}",
        "--sandbox",
        "workspace-write",
        "-c",
        "sandbox_workspace_write.exclude_slash_tmp=true",
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
