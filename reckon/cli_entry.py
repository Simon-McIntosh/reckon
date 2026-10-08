import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import click

from reckon import __version__
from reckon.hooks import install as hook_installer


def _asset_root() -> Path:
    """Resolve the canonical frontend assets from an install or source tree."""
    package_dir = Path(__file__).resolve().parent
    candidates = (package_dir / "_assets", package_dir.parent / "docs")
    required = {
        "ui": ("shell.jsx", "state-loader.js"),
        "_shared": ("foundation.css", "dashboard.css", "state.js", "badge.svg"),
    }
    for root in candidates:
        if (root / "index.html").is_file() and all(
            (root / directory).is_dir()
            and all((root / directory / name).is_file() for name in names)
            for directory, names in required.items()
        ):
            return root

    searched = ", ".join(str(path) for path in candidates)
    raise click.ClickException(
        "reckon frontend assets are missing or incomplete; "
        f"searched package and source locations: {searched}"
    )



def _skills_source() -> Path:
    """Resolve canonical skills from a source checkout or installed wheel."""

    package_dir = Path(__file__).resolve().parent
    candidates = (package_dir.parent / "skills", package_dir / "_skills")
    for candidate in candidates:
        if candidate.is_dir() and any(
            (path / "SKILL.md").is_file() for path in candidate.iterdir()
        ):
            return candidate
    searched = ", ".join(str(path) for path in candidates)
    raise click.ClickException(f"reckon skills are missing; searched: {searched}")



CREW_HOST_PLUGIN_NAME = "reckon-crew-host"



_CREW_HOST_PLUGIN_REL = Path("plugins") / "crew-host"



CREW_HOST_MISSING = "missing"



CREW_HOST_DANGLING = "dangling"



CREW_HOST_WORKTREE = "worktree"



CREW_HOST_ELSEWHERE = "elsewhere"



CREW_HOST_VALID = "valid"



class CrewHostLink(NamedTuple):
    """The state of the session-host plugin link and how it got there."""

    state: str
    detail: str
    dest: Path
    target: Path | None



def _reckon_checkout() -> Path:
    """The checkout this reckon package was imported from.

    A worker runs from a linked worktree, so this is the worktree root there
    and the main checkout everywhere else. ``_main_checkout`` collapses the two
    when the question is which tree a link should name.
    """
    return Path(__file__).resolve().parents[1]



def _git_capture(cwd: Path, *args: str) -> str | None:
    """Return git stdout when the command succeeds, and None otherwise."""
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        return None
    return result.stdout.strip() or None



def _main_checkout(checkout: Path) -> Path:
    """The main checkout a linked worktree belongs to, or ``checkout`` itself.

    ``reckon.crew.node.repository_identity`` owns this resolution: a linked
    worktree shares its repository's git common directory, whose parent is the
    main checkout root, so a worker running reckon from its own worktree still
    names the tree a user-level link must point at.
    """
    from reckon.crew.node import repository_identity

    resolved = repository_identity(checkout)
    return resolved if resolved is not None else checkout.resolve()



def _in_linked_worktree(path: Path) -> bool:
    """Whether ``path`` lives in a linked git worktree rather than a main tree.

    ``repository_identity`` answers which repository a path belongs to but
    collapses a worktree into its main checkout, so it cannot say whether the
    path it was given was the worktree. Comparing the path's own git toplevel
    against that collapsed root does: they differ exactly in a linked worktree.
    """
    from reckon.crew.node import repository_identity

    probe = path if path.is_dir() else path.parent
    main = repository_identity(probe)
    toplevel = _git_capture(probe, "rev-parse", "--show-toplevel")
    if main is None or not toplevel:
        return False
    return Path(toplevel).resolve() != main



CLAUDE_SKILLS_DIR_ENV = "RECKON_CLAUDE_SKILLS_DIR"



def _personal_skills_dir() -> Path:
    """The personal skills directory the session-host link resolves in.

    ``RECKON_CLAUDE_SKILLS_DIR`` overrides it when set, so a test or an
    operator can keep the link away from the real ``~/.claude/skills``; the
    Claude Code personal scope is the default.
    """
    override = os.environ.get(CLAUDE_SKILLS_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "skills"



def crew_host_link_state(skills_dir: Path, checkout: Path) -> CrewHostLink:
    """Report the state of the session-host plugin link.

    ``skills_dir`` is the personal skills directory (``~/.claude/skills``) and
    ``checkout`` is the main checkout whose ``plugins/crew-host`` the link
    should name. A link resolving into a worker worktree of the same repository
    is reported as ``worktree`` rather than as a merely wrong target, because
    the fix differs: the worktree link is one a reclaim would break.
    """
    dest = skills_dir / CREW_HOST_PLUGIN_NAME
    source = (checkout / _CREW_HOST_PLUGIN_REL).resolve()

    if not dest.is_symlink():
        if dest.exists():
            return CrewHostLink(
                CREW_HOST_ELSEWHERE,
                f"{dest} is not a symlink (note: a plugin must be linked)",
                dest,
                None,
            )
        return CrewHostLink(CREW_HOST_MISSING, f"no link at {dest}", dest, None)

    raw_target = os.readlink(dest)
    target = Path(raw_target)
    if not target.is_absolute():
        target = dest.parent / target

    if not dest.exists():
        return CrewHostLink(
            CREW_HOST_DANGLING,
            f"{dest} → {raw_target} (target missing)",
            dest,
            target,
        )

    resolved = dest.resolve()
    if _in_linked_worktree(resolved):
        return CrewHostLink(
            CREW_HOST_WORKTREE,
            f"{dest} → {resolved} (a worker worktree)",
            dest,
            resolved,
        )
    if resolved != source:
        return CrewHostLink(
            CREW_HOST_ELSEWHERE,
            f"{dest} → {resolved} (expected {source})",
            dest,
            resolved,
        )
    return CrewHostLink(CREW_HOST_VALID, f"{dest} → {resolved}", dest, resolved)



def link_crew_host_plugin(source: Path, skills_dir: Path) -> str:
    """Ensure ``skills_dir/reckon-crew-host`` links to ``source``.

    Idempotent: a link already naming the source is left alone. An existing
    symlink naming a different target is repointed. A real directory or file in
    the way is refused, because replacing it would discard whatever it holds.
    """
    dest = skills_dir / CREW_HOST_PLUGIN_NAME
    skills_dir.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink():
        current = Path(os.readlink(dest))
        if not current.is_absolute():
            current = dest.parent / current
        if current == source:
            return "ok"
        dest.unlink()
        dest.symlink_to(source, target_is_directory=True)
        return "repointed"
    if dest.exists():
        raise click.ClickException(
            f"{dest} exists and is not a symlink; remove it before linking "
            f"{CREW_HOST_PLUGIN_NAME}"
        )
    dest.symlink_to(source, target_is_directory=True)
    return "linked"



def _sync_crew_host_plugin() -> None:
    """Link the main checkout's session-host plugin during a sync.

    Run from a worker worktree this refuses and links nothing: the worktree is
    reclaimed when the run ends, so a link naming it would resolve to nothing.
    The refusal is reported and the rest of the sync proceeds, because the
    remaining scaffold is checkout-independent and a worker still needs it.
    """
    running = _reckon_checkout()
    main = _main_checkout(running)
    if running.resolve() != main:
        click.echo(
            f"  session-host plugin: refused — running from a worker worktree "
            f"({running}); run sync from the main checkout {main}"
        )
        return

    source = main / _CREW_HOST_PLUGIN_REL
    if not (source / ".claude-plugin" / "plugin.json").is_file():
        click.echo(f"  session-host plugin: not built under {source} — skipped")
        return

    skills_dir = _personal_skills_dir()
    action = link_crew_host_plugin(source, skills_dir)
    click.echo(
        f"  session-host plugin: {action} {skills_dir / CREW_HOST_PLUGIN_NAME} → {source}"
    )



def _claude_plugin_validate(dest: Path) -> str | None:
    """Validate a linked plugin with ``claude plugin validate`` when available.

    Returns ``None`` when the Claude CLI is not on PATH (nothing to run), an
    empty string when it validates, and its message when it does not.
    """
    exe = shutil.which("claude")
    if not exe:
        return None
    result = subprocess.run(
        [exe, "plugin", "validate", str(dest)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return ""
    return (result.stderr or result.stdout).strip() or f"exit {result.returncode}"



def _crew_guard_path(filename: str) -> Path:
    """Resolve an executable guard shipped with the installed package."""
    source_guard = (Path(__file__).resolve().parent / "hooks" / filename).resolve()
    guard = source_guard
    for entry in sys.path:
        if not entry:
            continue
        candidate = Path(entry).expanduser().resolve() / "reckon" / "hooks" / filename
        if candidate != source_guard and candidate.is_file():
            guard = candidate
            break
    if not guard.is_file() or not os.access(guard, os.X_OK):
        raise click.ClickException(
            f"harness guard is missing or not executable: {guard}"
        )
    return guard



def _native_agent_guard_path() -> Path:
    """Resolve the native-agent guard shipped with the installed package."""
    return _crew_guard_path("native_agent_guard.py")



def _worker_message_guard_path() -> Path:
    """Resolve the worker-message guard shipped with the installed package."""
    return _crew_guard_path("worker_message_guard.py")



def _worker_git_guard_path() -> Path:
    """Resolve the worker git guard shipped with the installed package."""
    return _crew_guard_path("worker_git_guard.py")



def _configure_crew_guards(
    settings_path: Path, *, remove: bool, include_git_guard: bool = False
) -> bool:
    """Install or remove sync's guard groups through the hook installer."""
    try:
        return hook_installer.configure_crew_guards(
            settings_path.expanduser().resolve(),
            remove=remove,
            include_git_guard=include_git_guard,
            guard_paths=(
                _native_agent_guard_path(),
                _worker_message_guard_path(),
                _worker_git_guard_path(),
            ),
        )
    except hook_installer.HookInstallError as exc:
        raise click.ClickException(str(exc)) from exc



def _crew_state_exists(docs_dir: Path, project: str) -> bool:
    """Return whether the synced project carries repository-local crew state."""
    state_dir = docs_dir / "state" / project
    return (state_dir / "crew.json").is_file() or (state_dir / "flight.yaml").is_file()



def _copied_where_linked(
    skills: list[Path], destination: Path
) -> list[tuple[Path, Path]]:
    """Find copied skill directories in an otherwise consistently linked set."""

    links = [destination / skill.name for skill in skills]
    link_parents = {
        path.resolve(strict=False).parent for path in links if path.is_symlink()
    }
    if len(link_parents) != 1:
        return []
    expected_root = link_parents.pop()
    drift: list[tuple[Path, Path]] = []
    for skill, path in zip(skills, links, strict=True):
        expected = expected_root / skill.name
        if (
            path.is_dir()
            and not path.is_symlink()
            and (expected / "SKILL.md").is_file()
        ):
            drift.append((path, expected))
    return drift



def _copy_asset_directory(source: Path, destination: Path) -> int:
    """Copy every top-level asset file, rejecting malformed destinations."""
    if destination.exists() and not destination.is_dir():
        raise click.ClickException(
            f"{destination.name} exists but is not a directory: {destination}"
        )
    source_files = [path for path in sorted(source.iterdir()) if path.is_file()]
    if not source_files:
        raise click.ClickException(f"frontend asset directory is empty: {source}")
    destination.mkdir(parents=True, exist_ok=True)
    for source_file in source_files:
        shutil.copy2(source_file, destination / source_file.name)
    return len(source_files)



def _merge_records_by_id(authored: list, discovered: list) -> list:
    """Supplement authored project records without replacing authored fields."""
    merged = [dict(item) for item in authored]
    positions = {
        item.get("id"): index
        for index, item in enumerate(merged)
        if isinstance(item, dict) and item.get("id")
    }
    for item in discovered:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        if item_id in positions:
            combined = dict(item)
            combined.update(merged[positions[item_id]])
            merged[positions[item_id]] = combined
        else:
            if item_id:
                positions[item_id] = len(merged)
            merged.append(dict(item))
    return merged



def _project_docs_root(project: str, checkout_path: Path | None = None) -> Path:
    """Resolve a project's docs root from mounts or an explicit checkout."""
    if checkout_path is not None:
        docs_dir = (checkout_path / "docs").resolve()
        if not docs_dir.is_dir():
            raise click.ClickException(
                f"cannot resolve checkout path docs directory: {docs_dir}"
            )
        return docs_dir

    from reckon._store import _mounts_path

    mounts_path = _mounts_path()
    if not mounts_path.exists():
        raise click.ClickException(
            "mounts.json not found; run `reckon sync` to register project roots"
        )
    try:
        mounts = json.loads(mounts_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise click.ClickException(
            f"cannot read mounts file {mounts_path}: {exc}"
        ) from exc
    raw = mounts.get(project)
    if raw is None:
        raise click.ClickException(
            f"project {project!r} is not mounted in {mounts_path}"
        )
    docs_dir = Path(raw).expanduser().resolve()
    if not docs_dir.is_dir():
        raise click.ClickException(
            f"mounted project path for {project!r} is not a directory: {docs_dir}"
        )
    return docs_dir



@click.group()
@click.version_option(version=__version__, prog_name="reckon")
def main():
    """reckon — repo-agnostic agile planning system."""

