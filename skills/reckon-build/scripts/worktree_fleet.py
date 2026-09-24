#!/usr/bin/env python3
"""Create and conservatively clean detached worktrees for delegated tasks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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


def worktree_roots(repo: Path) -> list[Path]:
    """Return this repository's worktree roots, preferred first.

    A worktree has to be reachable from wherever the node's work actually runs.
    Batch schedulers and remote hosts mount the repository's own filesystem but
    not the submitting machine's runtime temporary directory, which is typically
    node-local memory: a worktree created there is invisible to every job the
    node submits, and nothing the worker does inside it can fix that. The
    default therefore sits beside the repository and inherits exactly its
    visibility. Placing it there also keeps checkouts off memory-backed storage,
    where concurrent worktrees draw on the same pool as the processes reading
    them.

    ``RECKON_WORKTREE_ROOT`` overrides the location for a host that wants
    checkouts elsewhere. The runtime temporary directory is retained as a legacy
    root so sessions created under the previous default stay inspectable and
    removable rather than being stranded by this change.
    """
    git_dir = Path(run("git", "rev-parse", "--absolute-git-dir", cwd=repo))
    digest = hashlib.sha256(str(git_dir.resolve()).encode()).hexdigest()[:12]
    stem = f"{repo.name}-{digest}"
    override = os.environ.get("RECKON_WORKTREE_ROOT")
    preferred = (
        Path(override).expanduser().resolve()
        if override
        else repo.parent / ".reckon-worktrees"
    )
    worktree_markers = sum(
        part.lstrip(".") == "reckon-worktrees" for part in preferred.parts
    )
    if worktree_markers > 1:
        raise FleetError(
            "refusing to nest another reckon-worktrees root; dispatch from the "
            "owning checkout or set RECKON_WORKTREE_ROOT outside the current root"
        )
    roots = [preferred / stem]
    legacy = Path(tempfile.gettempdir()) / "reckon-worktrees" / stem
    if legacy != roots[0]:
        roots.append(legacy)
    return roots


def session_root(repo: Path, session: str) -> Path:
    return worktree_roots(repo)[0] / safe_token("session", session)


def session_roots(repo: Path, session: str) -> list[Path]:
    """Every root a session's worktrees may occupy, preferred first."""
    token = safe_token("session", session)
    return [root / token for root in worktree_roots(repo)]


def registered_worktrees(repo: Path) -> set[Path]:
    output = run("git", "worktree", "list", "--porcelain", cwd=repo)
    return {
        Path(line.removeprefix("worktree ")).resolve()
        for line in output.splitlines()
        if line.startswith("worktree ")
    }


def config_home() -> Path:
    """Resolve the configuration directory without importing the package."""
    override = os.environ.get("RECKON_HOME")
    if override:
        return Path(override).expanduser().resolve()
    preferred = Path.home() / ".config" / "reckon"
    if preferred.exists():
        return preferred
    return Path.home() / "docs-server"


def live_pointers() -> list[dict[str, object]]:
    """Read live run pointers directly from the transient crew directory."""
    directory = config_home() / "crew" / "live"
    if not directory.is_dir():
        return []
    pointers = []
    for path in sorted(directory.glob("*.json")):
        try:
            pointer = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(pointer, dict):
            pointers.append(pointer)
    return pointers


def process_alive(pid: object) -> bool | None:
    """Report OS liveness, or None when the pointer has no usable pid."""
    if not pid:
        return None
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (TypeError, ValueError):
        return None
    return True


def pointer_is_running(pointer: dict[str, object]) -> bool:
    """Apply the cleanup-relevant subset of live-run classification."""
    phase = str(pointer.get("phase") or "")
    if phase in ("complete", "failed", "stopped"):
        return False
    if phase:
        return True
    alive = (
        process_alive(pointer["pid"])
        if pointer.get("pid")
        else pointer.get("process_alive")
    )
    return alive is not False


def live_worktree_claims() -> dict[Path, list[str]]:
    """Return worktrees claimed by runs whose current classification is running."""
    claims: dict[Path, list[str]] = {}
    for pointer in live_pointers():
        if not pointer_is_running(pointer) or not pointer.get("worktree"):
            continue
        path = Path(str(pointer["worktree"])).resolve()
        claims.setdefault(path, []).append(str(pointer.get("run_id") or ""))
    return claims


ENV_LINKS = (".venv", ".env")


def provision_env_links(repo: Path, path: Path) -> dict[str, str]:
    """Point a worktree at the repository's shared environment.

    The repository's root ``.venv`` (and ``.env`` when present) is the one
    environment every worker shares; a detached worktree that resolves its own
    produces a second, expensive copy. There is no call site that reliably
    remembers this, so provisioning is bound to worktree creation — the moment
    that always happens. Only symlinks are made, never a directory copy and
    never in the reverse direction, and a link whose source is absent is
    skipped with its reason rather than failing creation. The bare ``.venv`` /
    ``.env`` entries are git-ignored, so a provisioned worktree still reports
    clean.
    """
    provisions: dict[str, str] = {}
    for name in ENV_LINKS:
        target = repo / name
        link = path / name
        if not target.exists():
            provisions[name] = f"skipped: no {name} at {target}"
            continue
        link.symlink_to(target)
        provisions[name] = f"linked -> {target}"
    return provisions


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
        "provisioned": provision_env_links(repo, path),
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
    roots = session_roots(repo, args.session)
    registered = registered_worktrees(repo)
    paths = sorted(path for path in registered if path.parent in set(roots))
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
        current_claims = live_worktree_claims().get(path.resolve(), [])
        if current_claims:
            raise FleetError(
                "cleanup refused; worktree gained a live claim: "
                f"{path} by {', '.join(sorted(current_claims))}"
            )
        run("git", "worktree", "remove", str(path), cwd=repo)
    run("git", "worktree", "prune", cwd=repo)
    for root in roots:
        if root.is_dir() and not any(root.iterdir()):
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
    roots = set(session_roots(repo, args.session))
    paths = sorted(path for path in registered_worktrees(repo) if path.parent in roots)
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
