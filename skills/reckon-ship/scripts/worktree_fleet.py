#!/usr/bin/env python3
"""Create and conservatively clean detached worktrees for delegated tasks."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path


SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class FleetError(RuntimeError):
    """A worktree lifecycle invariant was not satisfied."""


def run(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise FleetError(f"{' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def repository_root(value: str) -> Path:
    root = Path(value).resolve()
    resolved = Path(run("git", "rev-parse", "--show-toplevel", cwd=root)).resolve()
    if resolved != root:
        raise FleetError(f"--repo must be the repository root: {resolved}")
    return root


def safe_token(label: str, value: str) -> str:
    if not SAFE_TOKEN.fullmatch(value):
        raise FleetError(f"{label} must match {SAFE_TOKEN.pattern}")
    return value


def session_root(repo: Path, session: str) -> Path:
    git_dir = Path(run("git", "rev-parse", "--absolute-git-dir", cwd=repo))
    digest = hashlib.sha256(str(git_dir.resolve()).encode()).hexdigest()[:12]
    return (
        Path(tempfile.gettempdir())
        / "reckon-worktrees"
        / f"{repo.name}-{digest}"
        / safe_token("session", session)
    )


def registered_worktrees(repo: Path) -> set[Path]:
    output = run("git", "worktree", "list", "--porcelain", cwd=repo)
    return {
        Path(line.removeprefix("worktree ")).resolve()
        for line in output.splitlines()
        if line.startswith("worktree ")
    }


def live_worktree_claims() -> dict[Path, list[str]]:
    """Return worktrees claimed by runs whose current classification is running."""
    from reckon import crew

    claims: dict[Path, list[str]] = {}
    for pointer in crew.list_live():
        observed = dict(pointer)
        if observed.get("pid"):
            observed["process_alive"] = crew.process_alive(observed["pid"])
        report = crew.classify_pointer(observed)
        if report["classification"] != "running" or not report.get("worktree"):
            continue
        path = Path(str(report["worktree"])).resolve()
        claims.setdefault(path, []).append(str(report["run_id"]))
    return claims


def create(args: argparse.Namespace) -> dict[str, object]:
    repo = repository_root(args.repo)
    worker = safe_token("worker", args.worker)
    root = session_root(repo, args.session)
    path = root / worker
    if path.exists():
        raise FleetError(f"worktree path already exists: {path}")
    root.mkdir(parents=True, exist_ok=True)
    base_sha = run("git", "rev-parse", args.base, cwd=repo)
    run("git", "worktree", "add", "--detach", str(path), base_sha, cwd=repo)
    return {
        "ok": True,
        "action": "create",
        "repo": str(repo),
        "session": args.session,
        "worker": worker,
        "path": str(path),
        "base": args.base,
        "base_sha": base_sha,
    }


def inspect_worktree(
    repo: Path,
    path: Path,
    integrated_into: str,
    claimed_by_live_runs: Iterable[str] = (),
) -> dict[str, object]:
    resolved = path.resolve()
    if resolved not in registered_worktrees(repo):
        raise FleetError(f"path is not a registered worktree: {resolved}")
    dirty = run("git", "status", "--porcelain", cwd=resolved)
    head = run("git", "rev-parse", "HEAD", cwd=resolved)
    reachable = (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", head, integrated_into],
            cwd=repo,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )
    return {
        "path": str(resolved),
        "head": head,
        "clean": not dirty,
        "dirty": dirty.splitlines(),
        "integrated_into": integrated_into,
        "reachable": reachable,
        "claimed_by_live_runs": sorted(claimed_by_live_runs),
    }


def cleanup_session(args: argparse.Namespace) -> dict[str, object]:
    repo = repository_root(args.repo)
    root = session_root(repo, args.session)
    registered = registered_worktrees(repo)
    paths = sorted(path for path in registered if path.parent == root)
    claims = live_worktree_claims()
    reports = [
        inspect_worktree(
            repo,
            path,
            args.integrated_into,
            claims.get(path.resolve(), ()),
        )
        for path in paths
    ]
    unsafe = [
        report
        for report in reports
        if not report["clean"]
        or not report["reachable"]
        or report["claimed_by_live_runs"]
    ]
    if unsafe:
        raise FleetError(f"cleanup refused; unsafe worktrees: {json.dumps(unsafe)}")
    for path in paths:
        run("git", "worktree", "remove", str(path), cwd=repo)
    run("git", "worktree", "prune", cwd=repo)
    if root.exists():
        root.rmdir()
    return {
        "ok": True,
        "action": "cleanup-session",
        "repo": str(repo),
        "session": args.session,
        "integrated_into": args.integrated_into,
        "removed": [str(path) for path in paths],
    }


def inspect_session(args: argparse.Namespace) -> dict[str, object]:
    repo = repository_root(args.repo)
    root = session_root(repo, args.session)
    paths = sorted(path for path in registered_worktrees(repo) if path.parent == root)
    claims = live_worktree_claims()
    return {
        "ok": True,
        "action": "inspect-session",
        "repo": str(repo),
        "session": args.session,
        "worktrees": [
            inspect_worktree(
                repo,
                path,
                args.integrated_into,
                claims.get(path.resolve(), ()),
            )
            for path in paths
        ],
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    commands = result.add_subparsers(dest="command", required=True)

    create_parser = commands.add_parser("create")
    create_parser.add_argument("--repo", required=True)
    create_parser.add_argument("--session", required=True)
    create_parser.add_argument("--worker", required=True)
    create_parser.add_argument("--base", default="HEAD")
    create_parser.set_defaults(handler=create)

    for name, handler in (
        ("inspect-session", inspect_session),
        ("cleanup-session", cleanup_session),
    ):
        command = commands.add_parser(name)
        command.add_argument("--repo", required=True)
        command.add_argument("--session", required=True)
        command.add_argument("--integrated-into", default="HEAD")
        command.set_defaults(handler=handler)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        print(json.dumps(args.handler(args), indent=2, sort_keys=True))
    except FleetError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
