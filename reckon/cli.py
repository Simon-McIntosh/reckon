import json
import shutil
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

from reckon import __version__, pages
from reckon._store import _config_home


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


@main.group(name="agent-context")
def agent_context():
    """Inspect the effective agent instructions and skill metadata."""


@agent_context.command(name="doctor")
@click.option(
    "--target",
    required=True,
    type=click.Path(path_type=Path),
    help="File or directory the agent will work on.",
)
@click.option(
    "--agent",
    type=click.Choice(["codex", "claude"], case_sensitive=False),
    default="codex",
    show_default=True,
)
@click.option(
    "--user-home",
    type=click.Path(path_type=Path),
    default=None,
    help="Override the user home used for policy and skill discovery.",
)
@click.option(
    "--agent-root",
    type=click.Path(path_type=Path),
    default=None,
    help="Override the agent configuration root.",
)
@click.option(
    "--budget",
    type=click.IntRange(min=0),
    default=None,
    help="Override the project instruction byte budget.",
)
@click.option(
    "--activate-skill",
    "activated_skills",
    multiple=True,
    help="Record a skill body as explicitly activated.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit the complete JSON manifest.",
)
def agent_context_doctor(
    target, agent, user_home, agent_root, budget, activated_skills, as_json
):
    """Verify the instruction chain and context budget for TARGET."""
    from reckon.agent_context import ContextRequest, build_context_manifest

    request = ContextRequest(
        target=target,
        user_home=user_home or Path.home(),
        agent=agent,
        agent_root=agent_root,
        project_doc_max_bytes=budget,
        activated_skills=activated_skills,
    )
    try:
        manifest = build_context_manifest(request)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json:
        click.echo(json.dumps(manifest, indent=2, sort_keys=True))
    else:
        status = "PASS" if manifest["ok"] else "FAIL"
        click.echo(f"agent context: {status} ({manifest['agent']})")
        click.echo(f"  target:    {manifest['target']}")
        click.echo(f"  canonical: {manifest['canonical_policy']['path']}")
        entrypoint = manifest["entrypoint"]
        click.echo(f"  entrypoint: {entrypoint['path']} [{entrypoint['relationship']}]")
        chain = manifest["instructions"]["project_chain"]
        click.echo(f"  project instructions: {len(chain)}")
        for item in chain:
            click.echo(
                f"    {item['bytes']:>7} B  {item['sha256'][:12]}  {item['path']}"
            )
        budget_data = manifest["budget"]
        click.echo(
            "  budget: "
            f"{budget_data['project_bytes']}/{budget_data['limit_bytes']} B "
            f"({budget_data['remaining_bytes']} B remaining)"
        )
        skills = manifest["skills"]
        click.echo(
            f"  skills: {len(skills['discovered'])} metadata, "
            f"{len(skills['activated_bodies'])} activated bodies"
        )
        for finding in manifest["findings"]:
            click.echo(
                f"  {finding['severity'].upper()}: "
                f"{finding['code']} — {finding['path']}"
            )

    if not manifest["ok"]:
        raise click.exceptions.Exit(1)


@main.command()
@click.option("--port", default=8765, show_default=True, help="Port to listen on.")
@click.option(
    "--host",
    default=None,
    help="Bind address (default: $DOCS_SERVER_BIND or 127.0.0.1).",
)
@click.option(
    "--mounts",
    "mounts_file",
    default=None,
    type=click.Path(path_type=Path),
    help="Path to mounts.json.",
)
def serve(port, host, mounts_file):
    """Start the reckon server (HTTP + state store on port 8765)."""
    from reckon.serve import main as serve_main

    serve_main(port=port, host=host, mounts_file=mounts_file)


@main.command()
def mcp():
    """Start the reckon MCP server (stdio transport)."""
    from reckon.mcp import main as mcp_main

    mcp_main()


@main.command(name="badge")
@click.option("--project", required=True, help="Mounted project whose badge to render.")
@click.option(
    "--checkout-path",
    default=None,
    type=click.Path(path_type=Path),
    help="Repo root to read instead of the mounted checkout.",
)
@click.option("--write", "write_badge", is_flag=True, help="Update README.md in place.")
def badge_command(project, checkout_path, write_badge):
    """Print the badge for a repository that explicitly publishes its plans."""
    from reckon.badge import declared_badge, install_declared_badge

    docs_dir = _project_docs_root(project, checkout_path)
    try:
        markdown, strategy = declared_badge(docs_dir)
        if write_badge:
            install_declared_badge(docs_dir, strategy)
    except pages.PagesError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(markdown)


@main.command()
@click.option(
    "--project",
    default=None,
    help="Include this project's flight.yaml layer in the resolution.",
)
@click.option(
    "--checkout-path",
    default=None,
    type=click.Path(path_type=Path),
    help="Repo root to read the project layer from (for a worktree).",
)
@click.option(
    "--set",
    "overrides",
    multiple=True,
    metavar="KEY=VALUE",
    help="Override layer entry as a dotted key path; repeat as needed.",
)
@click.option(
    "--probe-auth",
    is_flag=True,
    help="Run each backend's declared auth_check and report its exit status.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def flight(project, checkout_path, overrides, probe_auth, pretty):
    """Resolve worker routing, gate strictness and fences across all layers.

    Prints one JSON object on stdout — the resolved config, the layer that
    supplied each key, and which backends are actually available — with keys in
    sorted order so two runs differ only where a value differs. Exits non-zero
    only when a layer is malformed, naming the file, key path and constraint.
    """
    import json

    from reckon.flight import FlightConfigError, flight_report, parse_overrides

    try:
        report = flight_report(
            project,
            overrides=parse_overrides(overrides) if overrides else None,
            probe_auth=probe_auth,
            checkout_path=checkout_path,
        )
    except FlightConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(json.dumps(report, indent=2 if pretty else None, sort_keys=True))


@main.command(name="capabilities")
@click.option(
    "--rebuild",
    is_flag=True,
    help="Rebuild the disposable cache from all mounted committed ledgers.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def capabilities_command(rebuild, pretty):
    """Inspect cache freshness, or explicitly rebuild it off dispatch."""

    from reckon import capabilities as capabilities_module

    if rebuild:
        record = capabilities_module.rebuild_capabilities()
        payload = {
            "rebuilt": True,
            "path": str(capabilities_module.capabilities_path()),
            "ledger_versions": record.get("ledger_versions", {}),
            "configurations": len(record.get("configurations") or []),
        }
    else:
        payload = {"rebuilt": False, **capabilities_module.inspect_capabilities()}
    _emit(payload, pretty)


@main.group(name="tag")
def tag():
    """Commands for resource tag operations."""


@tag.command(name="rename")
@click.option("--project", required=True, help="Project owning the tagged resources.")
@click.option(
    "--checkout-path",
    default=None,
    type=click.Path(path_type=Path),
    help=(
        "Optional repository checkout root for worktrees; defaults to mounted "
        "project path."
    ),
)
@click.option(
    "--dry-run", is_flag=True, help="Emit affected resources without writing."
)
@click.argument("source")
@click.argument("target")
def tag_rename(project, checkout_path, dry_run, source, target):
    """Rename a tag across every typed resource in the mounted project."""
    from reckon.tags import rename_project_tag

    docs_dir = _project_docs_root(project, checkout_path)
    report = rename_project_tag(
        docs_dir,
        project,
        source,
        target,
        dry_run=dry_run,
    )
    _emit(report, pretty=False)


@tag.command(name="backfill")
@click.option("--project", required=True, help="Project owning the resource corpus.")
@click.option(
    "--checkout-path",
    default=None,
    type=click.Path(path_type=Path),
    help=(
        "Optional repository checkout root for worktrees; defaults to mounted "
        "project path."
    ),
)
@click.option(
    "--preimage",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Saved source-to-destination layout move list.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report the complete census without writing resource files.",
)
def tag_backfill(project, checkout_path, preimage, dry_run):
    """Backfill topical tags from a preserved layout pre-image."""
    from tempfile import TemporaryDirectory

    from reckon.tags import (
        _contained_resource_path,
        _parse_layout_moves,
        backfill_tags_from_preimage,
    )

    docs_dir = _project_docs_root(project, checkout_path)
    if dry_run:
        moves = _parse_layout_moves(preimage.read_text(encoding="utf-8"))
        with TemporaryDirectory(
            dir=docs_dir.parent,
            prefix=".reckon-tag-backfill-",
        ) as preview_root:
            preview_docs = Path(preview_root) / "docs"
            for source, destination in moves:
                source_path = _contained_resource_path(docs_dir, source)
                destination_path = _contained_resource_path(docs_dir, destination)
                existing = next(
                    (
                        path
                        for path in (source_path, destination_path)
                        if path.is_file()
                    ),
                    None,
                )
                if existing is None:
                    continue
                preview_path = _contained_resource_path(preview_docs, source)
                preview_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(existing, preview_path)
            report = backfill_tags_from_preimage(preview_docs, preimage)
    else:
        report = backfill_tags_from_preimage(docs_dir, preimage)
    report.update(
        {
            "dry_run": dry_run,
            "written": 0 if dry_run else report["changed"],
        }
    )
    _emit(report, pretty=False)


@main.group(name="crew")
def crew():
    """Dispatch and observe workers through one backend-agnostic call.

    These are agent-callable primitives rather than a human interface: output is
    JSON on stdout, each call is atomic, nothing is interactive, and exit codes
    are branchable — 0 succeeded, 1 the configuration or request is wrong, 2 the
    node is not dispatchable and names which property it failed, 3 the wave is
    held on budget and names the backend, the utilisation and when it resets, 4
    the named plan section is unavailable at the worktree base ref, 5 the
    selected worker configuration has a typed competence refusal, 6 terminal
    pointers need reconciliation, 7 a live run holds a conflicting write scope,
    8 no live project watcher is waiting for the dispatched work.
    """


def _crew_modules():
    """Import the crew and flight helpers on demand."""
    from reckon import crew as crew_module
    from reckon import flight as flight_module

    return crew_module, flight_module


def _emit(payload, pretty: bool) -> None:
    """Print one JSON document, sorted so two runs diff only on real change."""
    click.echo(json.dumps(payload, indent=2 if pretty else None, sort_keys=True))


def _resolved_flight(flight_module, project, checkout_path, overrides):
    """Resolve flight config for a dispatch, prompt overrides winning."""
    try:
        return flight_module.resolve(
            project,
            overrides=flight_module.parse_overrides(overrides) if overrides else None,
            checkout_path=checkout_path,
        ).config
    except flight_module.FlightConfigError as exc:
        raise click.ClickException(str(exc)) from exc


def _repo_root(repo) -> Path:
    """Resolve the repository root a dispatch cuts its worktree from."""
    import subprocess

    if repo:
        return Path(repo).resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise click.ClickException(
            "not inside a git repository; pass --repo with the repository root"
        )
    return Path(result.stdout.strip()).resolve()


def _peer_scopes(values) -> dict:
    """Parse repeated ``name=path[,path]`` peer scope declarations."""
    peers: dict[str, list[str]] = {}
    for value in values:
        name, sep, paths = value.partition("=")
        if not sep or not name.strip():
            raise click.ClickException(
                f"--peer {value!r} must be written as name=path[,path]"
            )
        peers[name.strip()] = [
            part.strip() for part in paths.split(",") if part.strip()
        ]
    return peers


@crew.command(name="preflight")
@click.option("--project", required=True, help="Project whose run records are read.")
@click.option(
    "--role",
    "roles",
    multiple=True,
    help="Role taking part in the wave; its backend is checked. Repeat as needed.",
)
@click.option(
    "--backend",
    "backends",
    multiple=True,
    help="Backend to check by name, instead of resolving roles.",
)
@click.option(
    "--purpose",
    type=click.Choice(["dispatch", "resume"]),
    default="dispatch",
    show_default=True,
    help="A dispatch keeps back the resume reserve; a resume may spend it.",
)
@click.option(
    "--checkout-path",
    default=None,
    type=click.Path(path_type=Path),
    help="Repo root whose ledger and project flight layer are read.",
)
@click.option(
    "--set",
    "overrides",
    multiple=True,
    metavar="KEY=VALUE",
    help="Flight override for this check; always wins over config layers.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_preflight(project, roles, backends, purpose, checkout_path, overrides, pretty):
    """Decide, per backend, whether a wave may open — spending nothing to do it.

    Reads the budget signal that earlier runs already recorded, so the check
    costs no worker budget; a backend whose config sets ``budget_check`` also has
    its own account surface read, which runs no model either. Exits 3 when any
    backend is held, naming its utilisation and reset time; a backend reporting no
    headroom is never held, because absence of a signal is not exhaustion.
    """
    from reckon import budget as budget_module
    from reckon import ledger as ledger_module

    crew_module, flight_module = _crew_modules()
    config = _resolved_flight(flight_module, project, checkout_path, overrides)
    try:
        report = budget_module.preflight(
            project,
            config,
            backends=list(backends) or None,
            roles=list(roles) or None,
            root=checkout_path,
            purpose=purpose,
        )
        report["hold_history"] = budget_module.record_checks(
            project,
            report["backends"],
            root=checkout_path,
            resumption_fired=purpose == "resume",
        )
    except (crew_module.CrewError, ledger_module.LedgerError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    _emit({"ok": True, **report}, pretty)
    raise click.exceptions.Exit(3 if report["held"] else 0)


@crew.command(name="dispatch")
@click.option("--project", required=True, help="Project owning the plan.")
@click.option("--plan", "plan_slug", required=True, help="Plan slug the node serves.")
@click.option("--section", default="", help="Plan section the node implements.")
@click.option("--role", default="implement", show_default=True, help="Routing role.")
@click.option(
    "--spec-level",
    type=click.Choice(["exact", "guided", "open"]),
    default=None,
    help="Declared specification ownership level; omitted means undeclared.",
)
@click.option("--node", "node_id", required=True, help="Stable node id.")
@click.option("--goal", default="", help="The one deliverable this node produces.")
@click.option("--done-when", default="", help="The measure that emits evidence.")
@click.option(
    "--write-path",
    "write_paths",
    multiple=True,
    help="Exclusive write path; repeat for each.",
)
@click.option(
    "--peer",
    "peers",
    multiple=True,
    metavar="NAME=PATHS",
    help="Concurrent node and its paths, for the shared-file check.",
)
@click.option(
    "--requires-decision",
    "required_decisions",
    multiple=True,
    help="Decision key this node needs locked first.",
)
@click.option(
    "--locked-decision",
    "locked_decisions",
    multiple=True,
    help="Decision key already locked in the plan.",
)
@click.option("--time-budget", default="", help="Wall-clock allowance, e.g. 25m.")
@click.option(
    "--estimated-hours",
    type=click.FloatRange(min=0.0, min_open=True),
    default=None,
    help="Neutral worker-hours for this node; otherwise the plan estimate is labelled as fallback.",
)
@click.option("--manifest", default="", help="Manifest path the worker must write.")
@click.option(
    "--member",
    default="",
    help="Roster member to run this node, reusing its long-lived session.",
)
@click.option("--session", required=True, help="Opaque session id grouping worktrees.")
@click.option("--base", default="HEAD", show_default=True, help="Worktree base ref.")
@click.option(
    "--repo",
    default=None,
    type=click.Path(path_type=Path),
    help="Repository root (default: the enclosing repository).",
)
@click.option(
    "--checkout-path",
    default=None,
    type=click.Path(path_type=Path),
    help="Repo root to read the project flight layer from.",
)
@click.option(
    "--set",
    "overrides",
    multiple=True,
    metavar="KEY=VALUE",
    help=(
        "Override one flight key after file layers. For routed effort, "
        "roles.<role>.by_spec_level.<level>.effort overlays "
        "backends.<name>.effort."
    ),
)
@click.option(
    "--allow-execution-mismatch",
    is_flag=True,
    help=(
        "Dispatch despite an execution measure routed to a role declaring it "
        "cannot execute; the exception is recorded on the run."
    ),
)
@click.option(
    "--allow-unreconciled-runs",
    is_flag=True,
    help=(
        "Dispatch despite terminal run pointers older than the configured grace; "
        "the waived backlog is recorded on the new run."
    ),
)
@click.option(
    "--no-watch",
    is_flag=True,
    help=(
        "Dispatch without a live project watcher and record the explicit waiver "
        "on the run."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Validate and resolve only: no worktree, no process, no record.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_dispatch(
    project,
    plan_slug,
    section,
    role,
    spec_level,
    node_id,
    goal,
    done_when,
    write_paths,
    peers,
    required_decisions,
    locked_decisions,
    time_budget,
    estimated_hours,
    manifest,
    member,
    session,
    base,
    repo,
    checkout_path,
    overrides,
    allow_execution_mismatch,
    allow_unreconciled_runs,
    no_watch,
    dry_run,
    pretty,
):
    """Validate a node, resolve routing, and launch or prepare its worker.

    One instruction covers every backend. Which harness runs, at what model,
    effort and sandbox tier, is resolved from flight config — so this command
    names none of them, and the caller branches only on the returned
    ``launch`` kind.
    """
    crew_module, flight_module = _crew_modules()
    config = _resolved_flight(flight_module, project, checkout_path, overrides)

    node = crew_module.TaskNode(
        id=node_id,
        goal=goal,
        plan=plan_slug,
        section=section,
        role=role,
        spec_level=spec_level or "",
        done_when=done_when,
        write_paths=list(write_paths),
        time_budget=time_budget,
        estimated_hours=estimated_hours,
        manifest_path=manifest,
        requires_decisions=list(required_decisions),
        peer_scopes=_peer_scopes(peers),
    )

    if dry_run:
        try:
            resolution = crew_module.plan_dispatch(
                node=node,
                config=config,
                locked_decisions=locked_decisions,
                peer_scopes=node.peer_scopes,
                project=project,
                repo=_repo_root(repo),
                base=base,
                execution_override=allow_execution_mismatch,
                report_live_conflicts=True,
            )
        except crew_module.PlanVisibilityError as exc:
            _emit(
                {"ok": False, "error": "plan-unavailable", "detail": str(exc)},
                pretty,
            )
            raise click.exceptions.Exit(4) from exc
        except crew_module.CompetenceLimit as exc:
            _emit(
                {"ok": False, "error": "competence-refusal", "competence": exc.verdict},
                pretty,
            )
            raise click.exceptions.Exit(5) from exc
        except crew_module.CrewError as exc:
            _emit(
                {
                    "ok": False,
                    "dry_run": True,
                    "error": "dispatch-refused",
                    "detail": str(exc),
                },
                pretty,
            )
            click.echo(f"Error: {exc}", err=True)
            raise click.exceptions.Exit(1) from exc
        if resolution.competence and not resolution.competence["allowed"]:
            _emit(
                {
                    "ok": False,
                    "dry_run": True,
                    "error": "competence-refusal",
                    "competence": resolution.competence,
                },
                pretty,
            )
            raise click.exceptions.Exit(5)
        _emit(
            {"ok": resolution.validation.ok, "dry_run": True, **resolution.as_dict()},
            pretty,
        )
        raise click.exceptions.Exit(0 if resolution.validation.ok else 2)

    try:
        record = crew_module.dispatch(
            node=node,
            project=project,
            repo=_repo_root(repo),
            config=config,
            session=session,
            base=base,
            locked_decisions=locked_decisions,
            peer_scopes=node.peer_scopes,
            member=member,
            execution_override=allow_execution_mismatch,
            unreconciled_override=allow_unreconciled_runs,
            watch_required=True,
            watch_override=no_watch,
        )
    except crew_module.PlanVisibilityError as exc:
        _emit(
            {"ok": False, "error": "plan-unavailable", "detail": str(exc)},
            pretty,
        )
        raise click.exceptions.Exit(4) from exc
    except crew_module.BudgetHold as exc:
        # Held, not failed: nothing was created and the node is still ready, so
        # this exits on its own code rather than as an error the caller would
        # otherwise answer by reshaping work that is fine.
        _emit(
            {
                "ok": False,
                "error": "budget-hold",
                "detail": str(exc),
                "hold": exc.verdict,
            },
            pretty,
        )
        raise click.exceptions.Exit(3) from exc
    except crew_module.CompetenceLimit as exc:
        _emit(
            {"ok": False, "error": "competence-refusal", "competence": exc.verdict},
            pretty,
        )
        raise click.exceptions.Exit(5) from exc
    except crew_module.UnreconciledRuns as exc:
        _emit(
            {
                "ok": False,
                "error": "unreconciled-runs",
                "detail": str(exc),
                "runs": exc.runs,
            },
            pretty,
        )
        raise click.exceptions.Exit(6) from exc
    except crew_module.ScopeConflict as exc:
        _emit(
            {
                "ok": False,
                "error": "scope-conflict",
                "detail": str(exc),
                "run_id": exc.run_id,
                "node": exc.node_id,
                "candidate_path": exc.candidate_path,
                "claimed_path": exc.claimed_path,
            },
            pretty,
        )
        raise click.exceptions.Exit(7) from exc
    except crew_module.WatcherRequired as exc:
        _emit(
            {
                "ok": False,
                "error": "watcher-required",
                "detail": str(exc),
                "watch": exc.watch,
            },
            pretty,
        )
        raise click.exceptions.Exit(8) from exc
    except crew_module.MemberInFlight as exc:
        _emit(
            {
                "ok": False,
                "error": "member-in-flight",
                "detail": str(exc),
                "member": exc.member,
                "run_id": exc.run_id,
            },
            pretty,
        )
        raise click.exceptions.Exit(9) from exc
    except crew_module.CrewError as exc:
        if str(exc).startswith("node is not dispatchable"):
            _emit(
                {"ok": False, "error": "not-dispatchable", "detail": str(exc)}, pretty
            )
            raise click.exceptions.Exit(2) from exc
        # The caller reads stdout, so a refusal that writes only to stderr is
        # indistinguishable from a dispatch that produced nothing — and one
        # chained behind another in the same shell has the successor's status
        # to hide behind. Every refusal answers on the documented channel;
        # stderr keeps the sentence for whoever is reading a terminal.
        _emit({"ok": False, "error": "dispatch-refused", "detail": str(exc)}, pretty)
        click.echo(f"Error: {exc}", err=True)
        raise click.exceptions.Exit(1) from exc
    _emit({"ok": True, **record}, pretty)


@crew.command(name="shadow")
@click.option("--run", "run_id", required=True, help="Committed primary run id.")
@click.option(
    "--backend",
    required=True,
    help="Candidate backend name resolved through flight config.",
)
@click.option(
    "--set",
    "overrides",
    multiple=True,
    metavar="KEY=VALUE",
    help="Flight override for this shadow; always wins over config layers.",
)
@click.option(
    "--member",
    default="",
    help="Roster member to run the shadow, reusing its model-matched session.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Validate and resolve only: no worktree, process or live pointer.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_shadow(run_id, backend, overrides, member, dry_run, pretty):
    """Re-run a committed node at its original base as isolated evidence."""
    from reckon.crew.dispatch import shadow, shadow_source

    crew_module, flight_module = _crew_modules()
    repo = _repo_root(None)
    try:
        source = shadow_source(run_id, repo=repo)
        node = source["node"]
        routed_overrides = [*overrides, f"roles.{node.role}.backend={backend}"]
        if node.spec_level:
            routed_overrides.append(
                f"roles.{node.role}.by_spec_level.{node.spec_level}.backend={backend}"
            )
        config = _resolved_flight(
            flight_module,
            source["project"],
            repo,
            routed_overrides,
        )
        configuration_fields = {
            "launch",
            "model",
            "effort",
            "sandbox",
            "time_budget",
        }
        applicable_prefixes = {
            f"backends.{backend}",
            f"roles.{node.role}",
        }
        if node.spec_level:
            applicable_prefixes.add(
                f"roles.{node.role}.by_spec_level.{node.spec_level}"
            )
        configuration_overrides = set()
        for pair in overrides:
            key, separator, _value = pair.partition("=")
            prefix, _dot, field = key.rpartition(".")
            if (
                separator
                and prefix in applicable_prefixes
                and field in configuration_fields
            ):
                configuration_overrides.add(field)
        record = shadow(
            run_id,
            candidate_backend=backend,
            config=config,
            repo=repo,
            member=member,
            configuration_overrides=configuration_overrides,
            dry_run=dry_run,
        )
    except crew_module.PlanVisibilityError as exc:
        _emit(
            {"ok": False, "error": "plan-unavailable", "detail": str(exc)},
            pretty,
        )
        raise click.exceptions.Exit(4) from exc
    except crew_module.BudgetHold as exc:
        _emit(
            {
                "ok": False,
                "error": "budget-hold",
                "detail": str(exc),
                "hold": exc.verdict,
            },
            pretty,
        )
        raise click.exceptions.Exit(3) from exc
    except crew_module.CompetenceLimit as exc:
        _emit(
            {"ok": False, "error": "competence-refusal", "competence": exc.verdict},
            pretty,
        )
        raise click.exceptions.Exit(5) from exc
    except crew_module.UnreconciledRuns as exc:
        _emit(
            {
                "ok": False,
                "error": "unreconciled-runs",
                "detail": str(exc),
                "runs": exc.runs,
            },
            pretty,
        )
        raise click.exceptions.Exit(6) from exc
    except crew_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit({"ok": True, **record}, pretty)


@crew.command(name="attach")
@click.option("--run", "run_id", required=True, help="Run id returned by dispatch.")
@click.option("--task", required=True, help="The harness's own task identifier.")
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_attach(run_id, task, pretty):
    """Bind an in-harness dispatch to its prepared run record."""
    crew_module, _ = _crew_modules()
    try:
        record = crew_module.attach(run_id, task)
    except crew_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit({"ok": True, **record}, pretty)


@crew.command(name="observe")
@click.option("--run", "run_id", required=True, help="Run id to read from disk.")
@click.option("--project", default=None, help="Project whose flight layer applies.")
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_observe(run_id, project, pretty):
    """Fold a run's stream, manifest and liveness back into its record.

    Reports the phase, the captured session id and whatever budget signal the
    backend emitted — which may legitimately read ``unknown``. Absence of a
    signal is never reported as exhaustion.
    """
    crew_module, flight_module = _crew_modules()
    config = None
    if project:
        config = _resolved_flight(flight_module, project, None, ())
    try:
        record = crew_module.observe(run_id, config=config)
    except crew_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit({"ok": True, **record}, pretty)


def _follow_selects(
    event: Mapping[str, Any],
    *,
    session: str | None,
    run_ids: tuple[str, ...],
    attention: bool,
) -> bool:
    """Decide whether one transition belongs to this follower's fleet.

    An event whose owner cannot be established — a line a pre-upgrade producer
    rendered, or a pointer carrying no session — reaches every follower. A
    reader that drops what it cannot attribute converts an unknown into a
    silence, which is the failure this whole surface exists to prevent.
    """
    from reckon.crew.runs import WATCH_ATTENTION_STATES

    if event.get("legacy"):
        return True
    owner = str(event.get("session") or "")
    if session is not None and owner and owner != session:
        return False
    if run_ids and str(event.get("run_id") or "") not in run_ids:
        return False
    if attention and str(event.get("to_state") or "") not in WATCH_ATTENTION_STATES:
        return False
    return True


def _follow_watch_lines(
    project: str,
    *,
    session: str | None = None,
    run_ids: Iterable[str] = (),
    attention: bool = False,
    poll_interval: float = 0.1,
    sleeper=time.sleep,
    stop=None,
    on_poll=None,
):
    """Yield this follower's transitions for as long as its session lives.

    Two properties matter more than anything else here, and both were learned
    from measured silence:

    A follower outlives the producer it reads. The seat is released when a wave
    drains, so a follower that ended there covered exactly one wave — the next
    wave armed a fresh producer while the session's monitor had already exited.
    Waiting for the next producer instead makes one arming cover the session.

    A follower reports state, not lines. On re-attaching it re-derives the
    fleet and emits only what changed since it last spoke, so a reconnect is
    quiet when nothing moved and cannot swallow a transition that happened
    while no producer was up.
    """
    from reckon.crew import runs

    selected_runs = tuple(run_ids)
    reported: dict[str, str] = {}

    def _stopped() -> bool:
        return stop is not None and stop.is_set()

    def _tick() -> None:
        """Run the caller's per-wait work — reclaiming a registration, say."""
        if on_poll is not None:
            on_poll()

    def _emit(event: Mapping[str, Any]):
        """Return the event when it is both this follower's and news."""
        if not _follow_selects(
            event, session=session, run_ids=selected_runs, attention=attention
        ):
            return None
        run_id = str(event.get("run_id") or "")
        state = str(event.get("to_state") or "")
        if run_id and not event.get("legacy"):
            if reported.get(run_id) == state:
                return None
            reported[run_id] = state
        return event

    while not _stopped():
        if not runs.producer_live(project):
            _tick()
            sleeper(poll_interval)
            continue

        cursor = runs.watch_stream_cursor(project)
        # The baseline is the fleet report: one transition per live run, in the
        # ticker's own vocabulary. Nothing about the follower itself goes on this
        # stream — a reader wants worker transitions and the fleet posture, not
        # two streams interleaved into one pane.
        for event in cursor["baseline"]:
            selected = _emit(event)
            if selected is not None:
                yield selected
        stream_path = Path(cursor["stream_path"])

        while not stream_path.exists():
            if not runs.producer_live(project):
                break
            if _stopped():
                return
            _tick()
            sleeper(poll_interval)
        if not stream_path.exists():
            continue

        with stream_path.open(encoding="utf-8") as stream:
            stream.seek(cursor["offset"])
            while True:
                line = stream.readline()
                if line:
                    event = runs.parse_stream_line(line)
                    selected = None if event is None else _emit(event)
                    if selected is not None:
                        yield selected
                    continue
                # Stopping and losing what is already written would be the same
                # defect at the other end of the pipe, so both endings drain
                # the stream first.
                if _stopped():
                    return
                if not runs.producer_live(project):
                    break
                _tick()
                sleeper(poll_interval)


def _echo_follow_line(line: str, *, stream=None) -> None:
    """Write and flush one ticker line so pipe readers receive it immediately."""
    output = stream or click.get_text_stream("stdout")
    click.echo(line, file=output)
    output.flush()


@crew.command(name="follow")
@click.option("--project", required=True, help="Project whose watch stream to follow.")
@click.option(
    "--session",
    default=None,
    help=(
        "Deliver only the runs this session dispatched, and register the "
        "session as attached so dispatch can verify delivery."
    ),
)
@click.option(
    "--run",
    "run_ids",
    multiple=True,
    help="Deliver only these run ids. Repeat for several.",
)
@click.option(
    "--attention",
    is_flag=True,
    help=(
        "Deliver only states a coordinator must act on — terminal, blocked, "
        "stalled, stopped, abandoned — rather than every progress transition."
    ),
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Emit machine-readable transition objects rather than ticker lines.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_follow(project, session, run_ids, attention, json_output, pretty):
    """Follow one session's runs without acquiring the project's watcher seat.

    The seat is project-global and this delivery is session-local, so a
    follower is what a coordinator arms to be woken about its own fleet. Arm it
    through the host harness's per-line notification primitive: this command
    produces lines and does not exit, so a mechanism that reports only on exit
    delivers nothing at all.
    """
    from reckon.crew import runs as runs_module
    from reckon.crew.recovery import format_watch_transition

    delivery = runs_module.delivery_mode()

    def stream_events(registration):
        def poll() -> None:
            """Take over a registration whose holder has gone, while streaming.

            Silently. A second follower streaming read-only, and its later
            takeover, are facts about the followers rather than about the fleet.
            The session that needs telling is one trying to dispatch, and the
            dispatch guard tells it there, with the remedy.
            """
            if registration is None or registration.held:
                return
            registration.acquire()

        for event in _follow_watch_lines(
            project,
            session=session,
            run_ids=run_ids,
            attention=attention,
            on_poll=poll,
        ):
            if json_output:
                _emit({"ok": True, **event}, pretty)
            else:
                _echo_follow_line(
                    format_watch_transition(event, with_session=session is None)
                )

    try:
        if session is None:
            stream_events(None)
            return
        with runs_module.follower_registration(
            project,
            session,
            delivery=delivery,
            scope={"runs": list(run_ids), "attention": bool(attention)},
        ) as registration:
            stream_events(registration)
    except runs_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc


@crew.command(name="watch")
@click.option("--project", required=True, help="Project whose live fleet to watch.")
@click.option(
    "--stall-window",
    default="15m",
    show_default=True,
    help="Exit when a non-terminal run's stream stays quiet this long.",
)
@click.option(
    "--exit-on-empty",
    is_flag=True,
    help=(
        "With --once, exit when no live pointers remain instead of "
        "waiting for the first one."
    ),
)
@click.option(
    "--once",
    is_flag=True,
    help=(
        "Return after a single fleet event instead of following. The seat is "
        "released on return, so the caller must re-arm before its next dispatch."
    ),
)
@click.option(
    "--follow",
    is_flag=True,
    hidden=True,
    help=(
        "Accepted for compatibility; following is now the default, and this "
        "flag still wins over --once."
    ),
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Emit machine-readable transition objects rather than ticker lines.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_watch(project, stall_window, exit_on_empty, once, follow, json_output, pretty):
    """Follow a fleet through reconciliation, or return after one event.

    Following is the default because the watcher seat is what
    ``reckon crew dispatch`` requires. A seat released after every landing has
    to be re-armed before each dispatch, and a coordinator that forgets meets
    ``watcher-required`` instead of a worker. Following holds the seat from an
    empty project, through every landing, until the fleet drains.
    """
    crew_module, _ = _crew_modules()
    # --exit-on-empty only means anything to the single-event mode, so asking
    # for it selects that mode rather than being silently ignored.
    single_event = once or exit_on_empty
    try:
        if follow or not single_event:
            from reckon.crew.recovery import format_watch_transition, watch_follow

            for result in watch_follow(
                project, stall_window=stall_window, transitions=True
            ):
                if json_output or result.get("event") not in {"baseline", "transition"}:
                    _emit({"ok": True, **result}, pretty)
                else:
                    click.echo(format_watch_transition(result))
            return
        result = crew_module.watch(
            project,
            stall_window=stall_window,
            exit_on_empty=exit_on_empty,
        )
    except crew_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit({"ok": True, **result}, pretty)


@crew.command(name="unwatch")
@click.option("--project", required=True, help="Project whose watcher to stop.")
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_unwatch(project, pretty):
    """Stop one project's registered watcher and release its claim."""
    from reckon.crew.node import CrewError
    from reckon.crew.recovery import unwatch

    try:
        result = unwatch(project)
    except CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit({"ok": True, **result}, pretty)


@crew.command(name="list")
@click.option("--project", default=None, help="Return runs for one project only.")
@click.option("--phase", default=None, help="Return runs in one phase only.")
@click.option(
    "--session",
    default=None,
    help="Mark rows dispatched by this session as mine, and report its follower.",
)
@click.option("--mine", is_flag=True, help="Return only rows this --session owns.")
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_list(project, phase, session, mine, pretty):
    """List matching live run pointers, so a fresh session can pick them up.

    Every row names its owning session and, when one is supplied, whether that
    session owns it. The first question a recovering orchestrator asks is which
    of these runs are its own, and a node name is not evidence of ownership.
    """
    from reckon.crew.recovery import agent_label
    from reckon.crew.runs import project_watch_visibility

    if mine and session is None:
        raise click.ClickException("--mine needs --session to say whose runs to keep.")

    crew_module, _ = _crew_modules()
    project_records = crew_module.list_live(project=project)
    runs = []
    for record in project_records:
        if phase is not None and str(record.get("phase") or "") != phase:
            continue
        owner = str(record.get("session") or "") or None
        if mine and owner != session:
            continue
        classified = crew_module.classify_pointer(record)
        runs.append(
            {
                "run_id": record.get("run_id"),
                "node": (record.get("node") or {}).get("id"),
                "project": record.get("project"),
                "plan": (record.get("node") or {}).get("plan"),
                "session": owner,
                "member": record.get("member"),
                "agent": agent_label(record) or None,
                "mine": None if session is None else owner == session,
                "backend": record.get("backend"),
                "launch": record.get("launch"),
                "phase": record.get("phase"),
                "worktree": record.get("worktree"),
                "manifest_path": record.get("manifest_path"),
                "classification": classified.get("classification"),
                "process_alive": classified.get("process_alive"),
                "elapsed_seconds": classified.get("elapsed_seconds"),
                "log_age_seconds": classified.get("log_age_seconds"),
                "budget_seconds": classified.get("budget_seconds"),
                "budget_overrun": classified.get("budget_overrun"),
                "manifest_status": classified.get("manifest_status"),
                "next_action": classified.get("next_action"),
            }
        )
    payload = {"ok": True, "runs": runs}
    if project is not None:
        payload["watcher"] = project_watch_visibility(project, session=session)
    else:
        projects = sorted(
            {
                str(record.get("project") or "")
                for record in project_records
                if record.get("project")
            }
        )
        payload["watchers"] = [
            project_watch_visibility(project_name, session=session)
            for project_name in projects
        ]
    _emit(payload, pretty)


@crew.command(name="drain")
@click.option("--project", required=True, help="Project whose live pointers to drain.")
@click.option(
    "--leave",
    "leaves",
    multiple=True,
    metavar="RUN=DISPOSITION",
    help=(
        "Record why a live run remains: handed-off or still-working. "
        "Repeat for each deliberate remainder."
    ),
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_drain(project, leaves, pretty):
    """Report the session-closure drain over current live run pointers."""
    crew_module, _ = _crew_modules()
    requested = []
    for leave in leaves:
        run_id, separator, disposition = leave.partition("=")
        if not separator or not run_id.strip() or not disposition.strip():
            raise click.ClickException(f"--leave {leave!r} must be RUN=DISPOSITION")
        if disposition.strip() not in crew_module.RUN_DRAIN_DISPOSITIONS:
            allowed = ", ".join(crew_module.RUN_DRAIN_DISPOSITIONS)
            raise click.ClickException(
                f"run disposition {disposition.strip()!r} is not one of {allowed}"
            )
        requested.append((run_id.strip(), disposition.strip()))

    try:
        recorded = [
            crew_module.record_run_disposition(
                run_id,
                disposition,
                project=project,
            )
            for run_id, disposition in requested
        ]
        result = crew_module.drain(project)
    except crew_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit(
        {
            "ok": True,
            **result,
            "recorded": [
                {
                    "run_id": row.get("run_id"),
                    "disposition": row.get("closure_disposition"),
                }
                for row in recorded
            ],
        },
        pretty,
    )


@crew.command(name="gc")
@click.option(
    "--repo",
    default=None,
    type=click.Path(path_type=Path),
    help="Repository whose managed worktrees are inspected.",
)
@click.option(
    "--project",
    default=None,
    help="Project ledger used for transient run cleanup; all local ledgers by default.",
)
@click.option(
    "--integrated-into",
    default="HEAD",
    show_default=True,
    help="Revision that must contain a worktree commit before removal.",
)
@click.option(
    "--retention-days",
    type=click.IntRange(min=0),
    default=30,
    show_default=True,
    help="Keep promoted run directories for at least this many days.",
)
@click.option(
    "--apply",
    is_flag=True,
    help="Perform eligible removals; omission reports the exact dry run.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_gc(repo, project, integrated_into, retention_days, apply, pretty):
    """Report disposable crew workspaces, applying removals only on request."""
    crew_module, _ = _crew_modules()
    try:
        report = crew_module.garbage_collect(
            repo=_repo_root(repo),
            project=project,
            integrated_into=integrated_into,
            retention_days=retention_days,
            apply=apply,
        )
    except crew_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit({"ok": True, **report}, pretty)


@crew.command(name="resume")
@click.option("--run", "run_id", required=True, help="Run id to answer.")
@click.option("--advice", required=True, help="The orchestrator's answer.")
@click.option(
    "--print-only",
    is_flag=True,
    help="Show the resume invocation without running it.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_resume(run_id, advice, print_only, pretty):
    """Answer a stuck worker in the SAME session it got stuck in.

    Advice only makes sense to a worker that still remembers what it tried, so
    the resumed turn carries the prior context rather than restating it.
    """
    crew_module, flight_module = _crew_modules()
    try:
        record = crew_module.read_pointer(run_id)
        project = str(record.get("project") or "")
        config = (
            _resolved_flight(flight_module, project, record.get("repo"), ())
            if project
            else None
        )
        plan = crew_module.resume_plan(run_id, advice, config=config)
    except crew_module.BudgetHold as exc:
        _emit(
            {
                "ok": False,
                "error": "budget-hold",
                "detail": str(exc),
                "hold": exc.verdict,
            },
            pretty,
        )
        raise click.exceptions.Exit(3) from exc
    except crew_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    payload = {"ok": True, "run_id": run_id, **plan.as_dict()}
    if print_only:
        _emit(payload, pretty)
        return
    directory = crew_module.run_dir(run_id)
    turn = len(list(directory.glob("resume-*.jsonl"))) + 1
    advice_path = directory / f"resume-{turn}-advice.txt"
    advice_path.write_text(advice + "\n")
    log_path = directory / f"resume-{turn}.jsonl"
    stderr_path = directory / f"resume-{turn}.stderr.log"
    current = crew_module.read_pointer(run_id)
    manifest_baseline_mtime_ns = crew_module._manifest_mtime_ns(
        current.get("manifest_path") or ""
    )
    attempt_started_at = crew_module._utc_now()
    pid = crew_module._spawn(
        plan, log_path=log_path, stderr_path=stderr_path, prompt_path=advice_path
    )
    crew_module.record_resumption(
        run_id,
        pid=pid,
        turn=turn,
        log_path=log_path,
        stderr_path=stderr_path,
        attempt_started_at=attempt_started_at,
        manifest_baseline_mtime_ns=manifest_baseline_mtime_ns,
    )
    payload.update({"pid": pid, "log_path": str(log_path), "resumed_turn": turn})
    _emit(payload, pretty)


@crew.command(name="stop")
@click.option("--run", "run_id", required=True, help="Run id to stop.")
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_stop(run_id, pretty):
    """Stop a spawned run's process group and record that it was stopped."""
    crew_module, _ = _crew_modules()
    try:
        record = crew_module.terminate(run_id)
    except crew_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit({"ok": True, **record}, pretty)


@crew.command(name="discard")
@click.option("--run", "run_id", required=True, help="Run id to discard.")
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_discard(run_id, pretty):
    """Remove a non-running live pointer without promoting it."""
    crew_module, _ = _crew_modules()
    try:
        result = crew_module.discard(run_id)
    except crew_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit({"ok": True, **result}, pretty)


def _ledger_module():
    """Import the ledger helpers on demand."""
    from reckon import ledger as ledger_module

    return ledger_module


@crew.command(name="complete")
@click.option("--run", "run_id", required=True, help="Run id to promote.")
@click.option(
    "--gate",
    required=True,
    help="Gate verdict: passed, failed or not-run.",
)
@click.option(
    "--failure-classification",
    type=click.Choice(
        [
            "work-rejected",
            "correct-refusal",
            "malformed-node",
            "infrastructure-failure",
            "pre-existing-failure",
            "negative-result",
        ]
    ),
    default=None,
    help="Required for a failing gate; names what the failure measures.",
)
@click.option(
    "--commit",
    "commits",
    multiple=True,
    help="Commit the run landed; repeat for each.",
)
@click.option("--outcome", default="", help="One line on what the run produced.")
@click.option(
    "--no-commit",
    default="",
    help="Declare deliberately that a passing node produced no commit, and why. "
    "Recorded on the ledger row, so a commitless promotion is distinguishable "
    "from one that recorded nothing by accident.",
)
@click.option(
    "--tests-added",
    type=int,
    default=None,
    help="Tests this run added, for later calibration.",
)
@click.option(
    "--scope-changed",
    is_flag=True,
    help="The node's scope was widened mid-flight, so it measures neither the "
    "estimate nor the worker and is excluded from calibration.",
)
@click.option(
    "--completed-at",
    default="",
    help="Observed completion stamp, when promotion happens later.",
)
@click.option(
    "--checkout-path",
    default=None,
    type=click.Path(path_type=Path),
    help="Repo root whose ledger receives the record (default: the run's own).",
)
@click.option(
    "--gate-command",
    default="",
    help="The check a passing gate ran, e.g. the test command.",
)
@click.option(
    "--gate-exit-status",
    type=int,
    default=None,
    help="The check command's exit status.",
)
@click.option(
    "--gate-log-path",
    default="",
    help="Path to the check's captured output.",
)
@click.option(
    "--gate-log-digest",
    default="",
    help="Digest of the check's captured output, when the log itself is not kept.",
)
@click.option(
    "--waive-suite-delta",
    default="",
    metavar="REASON",
    help="Promote despite added suite failures and record the reason and delta.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_complete(
    run_id,
    gate,
    failure_classification,
    commits,
    outcome,
    no_commit,
    tests_added,
    scope_changed,
    completed_at,
    checkout_path,
    gate_command,
    gate_exit_status,
    gate_log_path,
    gate_log_digest,
    waive_suite_delta,
    pretty,
):
    """Promote a finished run into the owning repository's committed ledger.

    The ledger append happens before the pointer is deleted, so an interruption
    between them leaves a recoverable pointer rather than a lost record. A
    passing gate is refused unless it carries the check that produced it —
    this is the sole route by which a run is promoted, so the requirement is
    unconditional here even though the underlying ledger call remains
    permissive for internal callers assembling records from other evidence.
    """
    crew_module, _ = _crew_modules()
    ledger_module = _ledger_module()
    gate_check = {
        "command": gate_command,
        "exit_status": gate_exit_status,
        "log_path": gate_log_path,
        "log_digest": gate_log_digest,
    }
    try:
        result = crew_module.complete(
            run_id,
            gate=gate,
            failure_classification=failure_classification or "",
            commits=commits,
            outcome=outcome,
            no_commit=no_commit,
            tests_added=tests_added,
            scope_changed=scope_changed,
            completed_at=completed_at,
            root=checkout_path,
            gate_check=gate_check,
            require_gate_check=True,
            suite_delta_waiver=waive_suite_delta,
        )
    except ledger_module.SuiteDeltaError as exc:
        _emit(
            {
                "ok": False,
                "error": "suite-delta-refused",
                "detail": str(exc),
                "missing_fields": exc.missing_fields,
                "added_failure_ids": exc.added_failure_ids,
            },
            pretty,
        )
        raise click.exceptions.Exit(1) from exc
    except (crew_module.CrewError, ledger_module.LedgerError) as exc:
        raise click.ClickException(str(exc)) from exc
    _emit({"ok": True, **result}, pretty)


@crew.command(name="recover")
@click.option("--project", default=None, help="Limit to one project's runs.")
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_recover(project, pretty):
    """Classify every live pointer an interrupted orchestrator left behind.

    Reports running, completed-but-unpromoted (with its manifest path) and
    abandoned runs. It repairs the record only: no worktree is removed, no
    process is signalled, and nothing is promoted on its own initiative.
    """
    crew_module, flight_module = _crew_modules()
    config = None
    if project:
        config = _resolved_flight(flight_module, project, None, ())
    _emit({"ok": True, **crew_module.recover(project=project, config=config)}, pretty)


@crew.group(name="member")
def crew_member():
    """The project's committed team roster."""


@crew_member.command(name="add")
@click.option("--project", required=True, help="Project owning the roster.")
@click.option("--member", "member_id", required=True, help="Stable member id.")
@click.option("--harness", required=True, help="Backend this member dispatches to.")
@click.option("--role", default="implement", show_default=True, help="Routing role.")
@click.option(
    "--session",
    default="",
    help="Existing session id; omit so the first run captures one.",
)
@click.option(
    "--checkout-path",
    default=None,
    type=click.Path(path_type=Path),
    help="Repo root whose roster is written (default: the registered checkout).",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_member_add(project, member_id, harness, role, session, checkout_path, pretty):
    """Register a member, or update one already on the roster."""
    ledger_module = _ledger_module()
    try:
        entry = ledger_module.register_member(
            project,
            member_id,
            harness=harness,
            role=role,
            session_id=session or None,
            root=checkout_path,
        )
    except ledger_module.LedgerError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit({"ok": True, "project": project, "member": entry}, pretty)


@crew_member.command(name="list")
@click.option("--project", required=True, help="Project owning the roster.")
@click.option(
    "--checkout-path",
    default=None,
    type=click.Path(path_type=Path),
    help="Repo root whose roster is read (default: the registered checkout).",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_member_list(project, checkout_path, pretty):
    """List the project's roster, with the session each member reuses."""
    ledger_module = _ledger_module()
    try:
        roster = ledger_module.members(project, checkout_path)
    except ledger_module.LedgerError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit({"ok": True, "project": project, "members": roster}, pretty)


@crew.command(name="ledger")
@click.option("--project", required=True, help="Project whose ledger is read.")
@click.option(
    "--view",
    type=click.Choice(["summary", "records"]),
    default="summary",
    show_default=True,
    help="Rolled-up measures, or every completed record.",
)
@click.option(
    "--checkout-path",
    default=None,
    type=click.Path(path_type=Path),
    help="Repo root whose ledger is read (default: the registered checkout).",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_ledger(project, view, checkout_path, pretty):
    """Read the committed record of how this project's plans were implemented."""
    ledger_module = _ledger_module()
    try:
        if view == "records":
            payload = {
                "runs": ledger_module.runs(project, checkout_path),
                "holds": ledger_module.holds(project, checkout_path),
                "members": ledger_module.members(project, checkout_path),
            }
        else:
            payload = ledger_module.summary(project, root=checkout_path)
    except ledger_module.LedgerError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit({"ok": True, "project": project, **payload}, pretty)


@crew.command(name="repair-completion")
@click.option("--project", required=True, help="Project whose run ledger is checked.")
@click.option(
    "--write",
    "write_changes",
    is_flag=True,
    help="Persist re-derived completion measurements; the default only reports.",
)
@click.option(
    "--checkout-path",
    default=None,
    type=click.Path(path_type=Path),
    help="Repo root whose ledger is checked (default: the registered checkout).",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_repair_completion(project, write_changes, checkout_path, pretty):
    """Repair historical completion measurements from surviving run streams."""

    ledger_module = _ledger_module()
    try:
        report = ledger_module.repair_completion(
            project,
            root=checkout_path,
            write_changes=write_changes,
        )
    except ledger_module.LedgerError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit({"ok": True, **report}, pretty)


@main.group(name="service")
def service():
    """Run the reckon server as a systemd user service."""


def _service_module():
    """Import the service helpers, surfacing failures as CLI errors."""
    from reckon import service as service_module

    return service_module


def _service_call(action, *args, **kwargs):
    """Invoke a service helper, translating its errors into click errors."""
    module = _service_module()
    try:
        return action(module, *args, **kwargs)
    except module.ServiceError as error:
        raise click.ClickException(str(error)) from error


@service.command(name="install")
@click.option("--port", default=8765, show_default=True, help="Port to listen on.")
@click.option("--host", default=None, help="Bind address (default: 127.0.0.1).")
@click.option(
    "--mounts",
    "mounts_file",
    default=None,
    type=click.Path(path_type=Path),
    help="Path to mounts.json.",
)
@click.option(
    "--linger/--no-linger",
    default=True,
    show_default=True,
    help="Keep the service running after logout.",
)
@click.option(
    "--start/--no-start",
    default=True,
    show_default=True,
    help="Enable and start the unit once written.",
)
def service_install(port, host, mounts_file, linger, start):
    """Write the unit file and bring the server up under systemd."""

    def action(module):
        if not module.user_manager_running():
            raise module.ServiceError(
                "the per-user systemd manager is not running on this host"
            )
        if linger and not module.linger_enabled():
            module.enable_linger()
            click.echo("  enabled lingering (units survive logout)")
        was_active = (
            module.systemctl("is-active", module.UNIT_NAME, check=False).returncode == 0
        )
        path, changed = module.write_unit(port=port, host=host, mounts_file=mounts_file)
        click.echo(f"  {'wrote' if changed else 'unchanged'} {path}")
        module.systemctl("daemon-reload")
        if start:
            module.systemctl("enable", "--now", module.UNIT_NAME)
            click.echo(f"  enabled and started {module.UNIT_NAME}")
            # 'enable --now' leaves an already-running service on its old
            # definition, so a rewritten unit needs an explicit restart.
            if changed and was_active:
                module.systemctl("restart", module.UNIT_NAME)
                click.echo("  restarted onto the rewritten unit")
        if not linger and not module.linger_enabled():
            click.echo("  warning: lingering is off — the service stops at logout")

    _service_call(action)


@service.command(name="restart")
def service_restart():
    """Restart the server — the command to run after changing reckon code."""

    def action(module):
        module.require_installed()
        module.systemctl("restart", module.UNIT_NAME)
        click.echo(f"restarted {module.UNIT_NAME}")

    _service_call(action)


@service.command(name="start")
def service_start():
    """Start the server."""

    def action(module):
        module.require_installed()
        module.systemctl("start", module.UNIT_NAME)
        click.echo(f"started {module.UNIT_NAME}")

    _service_call(action)


@service.command(name="stop")
def service_stop():
    """Stop the server."""

    def action(module):
        module.require_installed()
        module.systemctl("stop", module.UNIT_NAME)
        click.echo(f"stopped {module.UNIT_NAME}")

    _service_call(action)


@service.command(name="status")
def service_status():
    """Show unit state, lingering, and the configured ExecStart."""

    def action(module):
        if not module.installed():
            click.echo(f"{module.UNIT_NAME}: not installed")
            click.echo("  run 'reckon service install' to deploy it")
            raise click.exceptions.Exit(1)
        active = module.systemctl("is-active", module.UNIT_NAME, check=False)
        enabled = module.systemctl("is-enabled", module.UNIT_NAME, check=False)
        click.echo(f"{module.UNIT_NAME}: {(active.stdout or '').strip() or 'unknown'}")
        click.echo(f"  enabled: {(enabled.stdout or '').strip() or 'unknown'}")
        click.echo(f"  linger:  {'yes' if module.linger_enabled() else 'no'}")
        click.echo(f"  unit:    {module.unit_path()}")
        for line in module.unit_path().read_text().splitlines():
            if line.startswith("ExecStart="):
                click.echo(f"  command: {line.removeprefix('ExecStart=')}")

    _service_call(action)


@service.command(name="logs")
@click.option(
    "-n",
    "--lines",
    default=50,
    show_default=True,
    help="Number of log lines to show.",
)
@click.option("-f", "--follow", is_flag=True, help="Stream new log lines.")
def service_logs(lines, follow):
    """Show the server's output."""
    import subprocess

    log_file = _service_module().log_path()
    if not log_file.is_file():
        click.echo(f"no log file yet: {log_file}")
        return
    argv = ["tail", "-n", str(lines)]
    if follow:
        argv.append("-f")
    argv.append(str(log_file))
    raise SystemExit(subprocess.run(argv, check=False).returncode)


@service.command(name="uninstall")
def service_uninstall():
    """Stop the server and remove its unit file."""

    def action(module):
        if not module.installed():
            click.echo(f"{module.UNIT_NAME}: not installed")
            return
        module.systemctl("disable", "--now", module.UNIT_NAME, check=False)
        module.unit_path().unlink()
        module.systemctl("daemon-reload")
        click.echo(f"removed {module.UNIT_NAME}")

    _service_call(action)


@main.command()
@click.argument("docs_path", type=click.Path(path_type=Path))
@click.option(
    "--project", default=None, help="Project key (defaults to docs parent dir name)."
)
@click.option(
    "--mounts",
    "mounts_file",
    default=None,
    type=click.Path(path_type=Path),
    help="Path to mounts.json.",
)
@click.option(
    "--state-root",
    default=None,
    type=click.Path(path_type=Path),
    help="State root dir.",
)
@click.option(
    "--generate-ci",
    is_flag=True,
    default=False,
    help="Opt into Pages publication and write a workflow when the strategy permits.",
)
def sync(docs_path, project, mounts_file, state_root, generate_ci):
    """Register a project and copy reckon UI files into its docs directory.

    DOCS_PATH is the path to the project's docs/ directory
    (or the directory where plan HTML pages live).

    reckon copies CSS, JSX, and state-loader from its own canonical source,
    registers the project in mounts.json, and creates a state directory.

    Plans are discovered live — the server scans HTML <meta name="plan-*">
    tags on every index.json request, so new plans appear immediately in the
    SPA without re-running sync.

    Run sync once to set up a new project, and again after a reckon update
    to pull in the latest CSS/JSX. It is NOT needed every time you add a plan.
    """
    docs_dir = docs_path.expanduser().resolve()
    if not docs_dir.exists():
        raise click.ClickException(f"docs path not found: {docs_dir}")

    publication_strategy = None
    if generate_ci:
        try:
            publication_strategy = pages.detect_publication_strategy(docs_dir)
        except pages.PagesError as exc:
            raise click.ClickException(str(exc)) from exc

    proj_name = project or docs_dir.parent.name
    asset_root = _asset_root()

    click.echo(f"Syncing {proj_name} → {docs_dir}")

    # ── Copy shared CSS + state.js ─────────────────────────────────────────
    shared_src = asset_root / "_shared"
    shared_dest = docs_dir / "_shared"
    shared_dest.mkdir(parents=True, exist_ok=True)
    for fname in ("foundation.css", "dashboard.css", "badge.svg"):
        src = shared_src / fname
        if src.is_file():
            shutil.copy2(src, shared_dest / fname)
            click.echo(f"  copied _shared/{fname}")

    # ── Write canonical index.html (SPA entry point) ──────────────────────
    index_html = docs_dir / "index.html"
    is_spa = index_html.is_file() and (
        "_shared/" in index_html.read_text() or "/_shared/" in index_html.read_text()
    )
    is_first_run = not index_html.exists()
    if is_first_run or is_spa:
        from reckon.serve import _render_spa_html

        template = _render_spa_html(
            proj_name,
            index_path=asset_root / "index.html",
        )
        index_html.write_text(template)
        click.echo(f"  wrote index.html (project={proj_name})")
    else:
        click.echo("  skipped index.html — not a reckon SPA (manual review)")

    # ── Drop .nojekyll (GitHub Pages) ─────────────────────────────────────
    nojekyll = docs_dir / ".nojekyll"
    if not nojekyll.exists():
        nojekyll.touch()
        click.echo("  created .nojekyll")

    # ── State directory + symlink ──────────────────────────────────────────
    ds_root = (state_root or _config_home() / "state").expanduser().resolve()
    ds_root.mkdir(parents=True, exist_ok=True)

    state_dir = docs_dir / "state" / proj_name
    state_dir.mkdir(parents=True, exist_ok=True)

    symlink = ds_root / proj_name
    if symlink.is_symlink():
        if symlink.resolve() != state_dir:
            symlink.unlink()
            symlink.symlink_to(state_dir)
            click.echo(f"  updated symlink {symlink} → {state_dir}")
        else:
            click.echo(f"  symlink ok: {symlink}")
    elif not symlink.exists():
        symlink.symlink_to(state_dir)
        click.echo(f"  symlink: {symlink} → {state_dir}")
    else:
        click.echo(f"  warning: {symlink} exists but is not a symlink — skipping")

    # ── Initialise project state without converting existing state ────────────
    index_json = state_dir / "index.json"
    from reckon.project_state import (
        create_project_state,
        enable_project_publication,
        project_state_mode,
    )

    if project_state_mode(docs_dir).format == "distributed":
        click.echo("  preserved frozen index.json (distributed project state)")
    elif index_json.is_file():
        click.echo("  preserved existing legacy index.json")
    else:
        created = create_project_state(docs_dir, proj_name)
        click.echo(
            "  created distributed project state "
            f"(resources={len(created['resources'])})"
        )

    if generate_ci:
        publication_version, publication_changed = enable_project_publication(
            docs_dir, proj_name
        )
        state = "recorded" if publication_changed else "already recorded"
        click.echo(
            f"  publication opt-in {state} (project version {publication_version})"
        )

    # ── Register in mounts.json ────────────────────────────────────────────
    mounts_path = (mounts_file or _config_home() / "mounts.json").expanduser()
    mounts_path.parent.mkdir(parents=True, exist_ok=True)
    mounts: dict = {}
    if mounts_path.exists():
        try:
            mounts = json.loads(mounts_path.read_text())
        except json.JSONDecodeError:
            pass
    if proj_name not in mounts:
        mounts[proj_name] = str(docs_dir)
        mounts_path.write_text(json.dumps(mounts, indent=2) + "\n")
        click.echo(f"  registered {proj_name} in {mounts_path}")
    else:
        click.echo(f"  {proj_name} already in mounts.json")

    # ── Generate CI workflow (optional) ───────────────────────────────────
    if generate_ci:
        repo_root = docs_dir.parent
        if publication_strategy.write_workflow:
            workflows_dir = repo_root / ".github" / "workflows"
            workflows_dir.mkdir(parents=True, exist_ok=True)
            ci_yml = workflows_dir / "reckon-pages.yml"
            ci_yml.write_text(_CI_WORKFLOW_TEMPLATE.format(docs_path=docs_path))
            click.echo(f"  wrote {ci_yml.relative_to(repo_root)}")
        else:
            click.echo(
                "  Pages publication: "
                f"{publication_strategy.describe()}; no workflow written"
            )
        try:
            badge_changed = pages.write_readme_badge(docs_dir, publication_strategy)
        except pages.PagesError as exc:
            raise click.ClickException(str(exc)) from exc
        if badge_changed:
            click.echo("  added README plans badge")
        elif publication_strategy.site_url is not None:
            click.echo("  README plans badge already current")

    click.echo(
        f"\nDone. Visit http://localhost:8765/{proj_name}/ once the server is running."
    )
    click.echo(
        'New plan pages appear live — the server discovers HTML <meta name="plan-*"> tags on every request.'
    )
    click.echo(
        "UI assets (JSX, CSS) are served directly from the reckon install — no per-project copies needed."
    )
    click.echo("Re-run sync only to update shared CSS after a reckon upgrade.")


# ── CI workflow template ────────────────────────────────────────────────────

_CI_WORKFLOW_TEMPLATE = """\
name: Deploy plans to GitHub Pages
on:
  push:
    branches: [main]
  workflow_dispatch:
permissions:
  contents: read
  pages: write
  id-token: write
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uvx --from "git+https://github.com/Simon-McIntosh/reckon@v0.2.0rc25" reckon build {docs_path}
      - uses: actions/upload-pages-artifact@v3
        with: {{ path: {docs_path} }}
  deploy:
    needs: build
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{{{ steps.deployment.outputs.page_url }}}}
    steps:
      - uses: actions/deploy-pages@v4
        id: deployment
"""


@main.command()
@click.argument("docs_path", type=click.Path(path_type=Path))
@click.option(
    "--project", default=None, help="Project key (defaults to docs parent dir name)."
)
def build(docs_path, project):
    """Bundle UI assets and generate a portable static site for CI/GitHub Pages.

    DOCS_PATH is the path to the project's docs/ directory.

    Copies the canonical frontend sources, compiles JSX into browser-ready JavaScript,
    writes local production runtimes and an index with relative asset paths, then writes
    complete project state so the SPA works without a running reckon server.

    Intended for CI (e.g. GitHub Actions). For local development, use reckon sync
    instead — it uses canonical server routes and doesn't need local asset copies.
    """
    docs_dir = docs_path.expanduser().resolve()
    if not docs_dir.exists():
        raise click.ClickException(f"docs path not found: {docs_dir}")

    proj_name = project or docs_dir.parent.name
    asset_root = _asset_root()

    click.echo(f"Building static site: {proj_name} → {docs_dir}")

    # ── Copy UI assets (JSX + CSS) ─────────────────────────────────────────
    ui_src = asset_root / "ui"
    ui_dest = docs_dir / "_ui"
    copied_ui = _copy_asset_directory(ui_src, ui_dest)
    click.echo(f"  copied _ui/ ({copied_ui} files)")

    from reckon.serve import _render_spa_html, client_runtime_assets, compile_jsx

    compiled_ui = 0
    for jsx_source in sorted(ui_src.glob("*.jsx")):
        output = ui_dest / f"{jsx_source.stem}.js"
        output.write_bytes(
            compile_jsx(
                jsx_source.read_text(encoding="utf-8"),
                filename=jsx_source.name,
            )
        )
        compiled_ui += 1
    click.echo(f"  compiled _ui/ ({compiled_ui} JSX modules)")

    runtime_dest = docs_dir / "_runtime"
    runtime_dest.mkdir(parents=True, exist_ok=True)
    runtimes = client_runtime_assets()
    for name, payload in runtimes.items():
        (runtime_dest / name).write_bytes(payload)
    click.echo(f"  wrote _runtime/ ({len(runtimes)} production bundles)")

    # ── Copy shared CSS + state.js ─────────────────────────────────────────
    shared_src = asset_root / "_shared"
    shared_dest = docs_dir / "_shared"
    copied_shared = _copy_asset_directory(shared_src, shared_dest)
    click.echo(f"  copied _shared/ ({copied_shared} files)")

    # ── Generate index.html with RELATIVE paths ────────────────────────────
    index_html = docs_dir / "index.html"
    index_html.write_text(
        _render_spa_html(
            proj_name,
            relative_assets=True,
            index_path=asset_root / "index.html",
        )
    )
    click.echo(f"  wrote index.html (project={proj_name}, relative paths)")

    # ── Drop .nojekyll ─────────────────────────────────────────────────────
    nojekyll = docs_dir / ".nojekyll"
    if not nojekyll.exists():
        nojekyll.touch()
        click.echo("  created .nojekyll")

    # ── Discover plans + write index.json with full inventory ──────────────
    # Static deployments have no live server, so we bake inventory into index.json.
    from reckon.serve import discover_plans

    state_dir = docs_dir / "state" / proj_name
    state_dir.mkdir(parents=True, exist_ok=True)
    discovered = discover_plans(docs_dir, proj_name, docs_dir / "state")

    index_json = state_dir / "index.json"
    from reckon.project_state import compose_project_state, project_state_mode

    distributed = project_state_mode(docs_dir).format == "distributed"
    idx_data: dict = {}
    if distributed:
        idx_data = compose_project_state(docs_dir, proj_name)
    elif index_json.is_file():
        try:
            env = json.loads(index_json.read_text())
            idx_data = dict(env.get("data", {}))
        except json.JSONDecodeError:
            pass

    idx_data["inventory"] = discovered["inventory"]
    idx_data["sprints"] = _merge_records_by_id(
        idx_data.get("sprints") or [], discovered["sprints"]
    )
    idx_data["milestones"] = _merge_records_by_id(
        idx_data.get("milestones") or [], discovered["milestones"]
    )
    if not idx_data.get("active_sprint_id"):
        active = next(
            (s for s in idx_data["sprints"] if s.get("status") == "active"), None
        )
        if active:
            idx_data["active_sprint_id"] = active["id"]
    if not distributed:
        idx_data["_version"] = (idx_data.get("_version") or 0) + 1

    envelope = {
        "updated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
        "project": proj_name,
        "doc": "projection" if distributed else "index",
        "data": idx_data,
    }
    output_state = state_dir / ("projection.json" if distributed else "index.json")
    output_state.write_text(json.dumps(envelope, indent=2) + "\n")
    n_plans = len(idx_data["inventory"])
    n_sprints = len(idx_data["sprints"])
    click.echo(
        f"  wrote state/{proj_name}/{output_state.name} "
        f"({n_plans} plans, {n_sprints} sprints)"
    )

    click.echo(
        f"\nBuild complete. Deploy the {docs_dir.name}/ directory as a static site."
    )


@main.command(name="stamp-superseded-state")
@click.option("--project", required=True, help="Project whose aggregate to stamp.")
@click.option(
    "--docs-path",
    default=None,
    help="Docs directory to act on. Defaults to the project's registered mount.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def stamp_superseded_state(project, docs_path, pretty):
    """Mark a migrated project's legacy index.json as superseded, in the file.

    Activation does this from now on. This repairs a project migrated before it
    did: the aggregate keeps the shape of a live resource, so a reader who opens
    it has nothing telling them the sprints, milestones and blockers moved to
    independently versioned resources — one read a stale sprint list and
    reported false state. Idempotent, and it never touches `data`.
    """
    from reckon._store import _docs_dir_for_project
    from reckon.project_state import ProjectStateError, stamp_legacy_index

    docs_dir = (
        Path(docs_path).expanduser().resolve()
        if docs_path
        else _docs_dir_for_project(project)
    )
    if docs_dir is None or not docs_dir.is_dir():
        raise click.ClickException(
            f"no readable docs directory for project {project!r}; pass --docs-path"
        )
    try:
        result = stamp_legacy_index(docs_dir, project)
    except ProjectStateError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit(result, pretty)


@main.command(name="migrate-layout")
@click.argument("docs_path", type=click.Path(path_type=Path))
@click.option(
    "--project", default=None, help="Project key (defaults to docs parent dir name)."
)
@click.option(
    "--check",
    is_flag=True,
    default=False,
    help="Preflight and print the deterministic move set without changing files.",
)
def migrate_layout(docs_path, project, check):
    """Explicitly migrate flat HTML resources into canonical typed roots."""
    from reckon.resources import (
        ResourceCollision,
        build_migration_manifest,
        migrate_typed_layout,
        migration_paths,
    )

    docs_dir = docs_path.expanduser().resolve()
    if not docs_dir.is_dir():
        raise click.ClickException(f"docs path not found: {docs_dir}")
    proj_name = project or docs_dir.parent.name
    try:
        manifest = (
            build_migration_manifest(docs_dir, proj_name)
            if check
            else migrate_typed_layout(docs_dir, proj_name)
        )
    except ResourceCollision as exc:
        raise click.ClickException(str(exc)) from exc

    moves = list(migration_paths(manifest))
    for source, destination in moves:
        click.echo(f"  {source} -> {destination}")
    verb = "would move" if check else "moved"
    click.echo(f"{verb} {len(moves)} resource(s)")
    if not check:
        click.echo(f"manifest: {docs_dir / '.reckon/typed-resource-manifest.json'}")


@main.command()
def doctor():
    """Verify reckon installation health.

    Checks:
    - Skills installed at ~/.claude/skills/reckon-*/
    - mounts.json reachable (default: ~/docs-server/mounts.json)
    - Every mounted project directory exists
    - Reckon MCP registration present in Claude Desktop or Codex config

    Prints a green checkmark on pass or a named fix suggestion on fail.
    """
    import sys

    ok = True
    skills = sorted(
        path.name
        for path in _skills_source().iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    )
    skills_dir = Path.home() / ".claude" / "skills"

    click.echo("reckon doctor\n")

    # ── Skills check ────────────────────────────────────────────────────────
    click.echo("Skills")
    for skill in skills:
        skill_path = skills_dir / skill / "SKILL.md"
        if skill_path.is_file():
            click.echo(f"  ✓  {skill}")
        else:
            click.echo(f"  ✗  {skill}  →  run: reckon install-skills", err=False)
            ok = False

    # ── mounts.json check ───────────────────────────────────────────────────
    click.echo("\nMounts")
    mounts_path = _config_home() / "mounts.json"
    if not mounts_path.exists():
        click.echo(f"  ✗  mounts.json not found at {mounts_path}")
        click.echo(f"       create it:  echo '{{}}' > {mounts_path}")
        ok = False
    else:
        try:
            mounts = json.loads(mounts_path.read_text())
            click.echo(
                f"  ✓  mounts.json  ({len(mounts)} project{'s' if len(mounts) != 1 else ''})"
            )
            for name, path in mounts.items():
                p = Path(path).expanduser()
                if p.is_dir():
                    click.echo(f"  ✓  mount [{name}] → {p}")
                else:
                    click.echo(f"  ✗  mount [{name}] → {p}  (directory not found)")
                    ok = False
        except (json.JSONDecodeError, OSError) as e:
            click.echo(f"  ✗  mounts.json unreadable: {e}")
            ok = False

    # ── MCP config check ─────────────────────────────────────────────────────
    click.echo("\nMCP config")
    claude_candidates = [
        Path.home() / ".claude" / "claude_desktop_config.json",
        Path.home() / ".config" / "claude" / "claude_desktop_config.json",
    ]
    codex_candidate = Path.home() / ".codex" / "config.toml"
    mcp_registration = None
    config_errors: list[str] = []
    for candidate in claude_candidates:
        if not candidate.is_file():
            continue
        try:
            cfg = json.loads(candidate.read_text())
            if "reckon" in cfg.get("mcpServers", {}):
                mcp_registration = candidate
                break
        except (json.JSONDecodeError, OSError) as e:
            config_errors.append(f"{candidate.name} unreadable: {e}")

    if mcp_registration is None and codex_candidate.is_file():
        try:
            import tomllib

            cfg = tomllib.loads(codex_candidate.read_text())
            if "reckon" in cfg.get("mcp_servers", {}):
                mcp_registration = codex_candidate
        except (OSError, tomllib.TOMLDecodeError) as e:
            config_errors.append(f"{codex_candidate.name} unreadable: {e}")

    if mcp_registration is not None:
        click.echo(f"  ✓  MCP server 'reckon' registered in {mcp_registration.name}")
    else:
        click.echo(
            "  ✗  MCP server 'reckon' is not registered in Claude Desktop or Codex"
        )
        for error in config_errors:
            click.echo(f"       {error}")
        click.echo("       see: https://docs.reckon.dev/mcp")
        ok = False

    # ── Summary ──────────────────────────────────────────────────────────────
    click.echo("")
    if ok:
        click.echo("All checks passed.")
    else:
        click.echo("Some checks failed — see fixes above.", err=False)
        sys.exit(1)


@main.command()
@click.option(
    "--project",
    default=None,
    help="Limit the lifecycle audit to one mounted project.",
)
def audit(project):
    """Report stale lifecycle state across mounted reckon projects.

    Flags:
      - STALE: active plans older than 30 days with impl < 1.0
      - MISSING_IMPL: shipped/done plans with missing or zero impl
      - STALE_RCA: research docs older than 60 days that are not done/archived

    Exits 1 when any MISSING_IMPL row is found (CI-friendly).
    """
    import sys

    from reckon.doccheck import audit_lifecycle

    try:
        findings = audit_lifecycle(project=project)
    except ValueError as e:
        raise click.ClickException(str(e)) from e

    if not findings:
        click.echo("No lifecycle hygiene findings.")
        return

    rows = [
        (
            item.project,
            item.slug,
            item.flag,
            f"{item.age_days}d",
            "-" if item.impl is None else f"{item.impl:.2f}",
            item.last_modified,
        )
        for item in findings
    ]
    headers = ("project", "plan-slug", "flag", "age", "impl", "last-modified")
    widths = [
        max(len(header), *(len(row[idx]) for row in rows))
        for idx, header in enumerate(headers)
    ]
    fmt = "  ".join(f"{{:<{width}}}" for width in widths)
    click.echo(fmt.format(*headers))
    click.echo(fmt.format(*("-" * width for width in widths)))
    for row in rows:
        click.echo(fmt.format(*row))

    if any(item.flag == "MISSING_IMPL" for item in findings):
        sys.exit(1)


@main.command(name="archive")
@click.option("--project", required=True, help="Mounted project key.")
@click.option(
    "--older-than-days",
    required=True,
    type=click.IntRange(min=0),
    help="Archive terminal documents whose age exceeds this configured threshold.",
)
@click.option(
    "--apply",
    "apply_changes",
    is_flag=True,
    help="Set plan-archived=1 after printing the complete candidate list.",
)
@click.option(
    "--checkout-path",
    default=None,
    type=click.Path(path_type=Path),
    help="Repository root for a worktree-specific project pass.",
)
def archive(project, older_than_days, apply_changes, checkout_path):
    """Preview or apply age-based archival of done and superseded documents."""
    from reckon.archive import ArchiveConfig, ArchiveError, run_archive_pass

    docs_dir = _project_docs_root(project, checkout_path)

    def report_candidates(candidates):
        if not candidates:
            click.echo("No archive candidates.")
            return
        rows = [
            (item.slug, item.status, f"{item.age_days}d", item.relative_path)
            for item in candidates
        ]
        headers = ("slug", "status", "age", "path")
        widths = [
            max(len(header), *(len(row[index]) for row in rows))
            for index, header in enumerate(headers)
        ]
        row_format = "  ".join(f"{{:<{width}}}" for width in widths)
        click.echo(row_format.format(*headers))
        click.echo(row_format.format(*("-" * width for width in widths)))
        for row in rows:
            click.echo(row_format.format(*row))

    try:
        result = run_archive_pass(
            docs_dir,
            project,
            ArchiveConfig(older_than_days=older_than_days),
            apply=apply_changes,
            reporter=report_candidates,
        )
    except (ArchiveError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    if apply_changes:
        click.echo(f"Archived {len(result.archived)} document(s).")
    else:
        click.echo(f"Dry run: {len(result.candidates)} candidate(s); no files changed.")


@main.group(name="evidence")
def evidence():
    """Compose closure evidence from durable project records."""


@evidence.command(name="synthesize")
@click.option("--project", required=True, help="Mounted project key.")
@click.option("--plan", "plan_slug", required=True, help="Plan slug to summarize.")
@click.option(
    "--checkout-path",
    default=None,
    type=click.Path(path_type=Path),
    help="Repository root for a worktree-specific synthesis.",
)
def synthesize_evidence(project, plan_slug, checkout_path):
    """Write canonical landed evidence from comments and the run ledger."""
    from reckon.evidence import EvidenceSynthesisError, synthesize_landed_record

    docs_dir = _project_docs_root(project, checkout_path)
    try:
        result = synthesize_landed_record(docs_dir, project, plan_slug)
    except EvidenceSynthesisError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"Synthesized {result.path} from {result.runs} run(s), "
        f"{result.comments} comment(s), and {result.commits} commit(s)."
    )


def _print_roadmap_report(report: dict) -> None:
    project = report.get("project", "")
    completion = report.get("completion", {})
    click.echo(
        f"{project}: {completion.get('lifecycle_completion_pct', 0):.1f}% lifecycle, "
        f"{completion.get('implementation_pct', 0):.1f}% implementation; "
        f"{len(report.get('ready_now', []))} ready, "
        f"{len(report.get('blocked', []))} blocked, "
        f"{len(report.get('deferred', []))} deferred"
    )
    critical = report.get("critical_path", {}).get("plans", [])
    if critical:
        click.echo("  critical: " + " -> ".join(critical))
    for item in report.get("immediate_roadmap", []):
        click.echo(f"  {item.get('order')}. {item.get('slug')} — {item.get('reason')}")
    for finding in report.get("wiring_findings", []):
        if finding.get("severity") in {"error", "warn"}:
            click.echo(
                f"  {str(finding.get('severity')).upper()} "
                f"{finding.get('code')}: {finding.get('message')}"
            )


@main.command()
@click.option(
    "--project",
    default="*",
    show_default=True,
    help="Mounted project key, or * for the portfolio.",
)
@click.option(
    "--sprint", default=None, help="Limit to one sprint and its prerequisites."
)
@click.option(
    "--checkout-path",
    default=None,
    type=click.Path(path_type=Path),
    help="Repository root for a worktree-specific single-project scan.",
)
@click.option("--max-paths", default=5, show_default=True, type=click.IntRange(1, 50))
@click.option("--json-output", is_flag=True, help="Emit the lossless JSON report.")
def roadmap(project, sprint, checkout_path, max_paths, json_output):
    """Show pending work, blockers, sprint progress, and critical paths."""

    from reckon.mcp import _roadmap

    result = _roadmap(
        project,
        str(checkout_path.resolve()) if checkout_path else None,
        sprint,
        max_paths,
    )
    if not result.get("ok", True):
        raise click.ClickException(str(result.get("detail") or result.get("error")))
    if json_output:
        click.echo(json.dumps(result, indent=2))
        return
    if project == "*":
        portfolio = result.get("portfolio", {})
        click.echo(
            f"portfolio: {portfolio.get('lifecycle_completion_pct', 0):.1f}% lifecycle, "
            f"{portfolio.get('implementation_pct', 0):.1f}% implementation; "
            f"{portfolio.get('ready', 0)} ready, "
            f"{portfolio.get('blocked', 0)} blocked, "
            f"{portfolio.get('deferred', 0)} deferred"
        )
        for report in result.get("projects", []):
            if report.get("ok", True):
                _print_roadmap_report(report)
            else:
                click.echo(
                    f"{report.get('project')}: ERROR "
                    f"{report.get('detail') or report.get('error')}"
                )
        return
    _print_roadmap_report(result)


@main.command(name="audit-doc")
@click.argument("paths", nargs=-1, required=True, type=click.Path(path_type=Path))
@click.option(
    "--project",
    default=None,
    help="Project key for image-path checks (default: <meta name=docs-project>).",
)
@click.option(
    "--check-links",
    is_flag=True,
    default=False,
    help="Also check internal links for dangling targets (corpus-aware).",
)
def audit_doc(paths, project, check_links):
    """Validate authored plan/doc HTML against the SPA render contract.

    The reckon SPA renders authored HTML faithfully (raw-HTML passthrough): no
    markdown is rendered, the doc's <head><style> is dropped, and images resolve
    against the project mount (/<project>/figures/...). This command flags docs
    that rely on markdown, head-local CSS, or relative image paths — problems
    that render wrong in the SPA.

    With --check-links, also verifies that internal <a href> links and
    plan-depends-on/plan-blocks/plan-informs slug references resolve to existing
    doc files and in-page anchors. Requires all audited docs to live in the same
    docs directory (corpus is built from that directory).

    Exits non-zero if any ERROR-level problem is found (relative <img src>,
    literal **markdown** in a rendered body, missing required meta).

    Example:

        reckon audit-doc docs/my-plan.html
        reckon audit-doc docs/*.html
        reckon audit-doc docs/*.html --check-links
    """
    import sys

    from reckon.doccheck import run

    sys.exit(run([str(p) for p in paths], project=project, check_links=check_links))


@main.command(name="install-skills")
@click.option(
    "--repair",
    is_flag=True,
    help="Replace copied-where-linked skill directories with the expected symlink.",
)
def install_skills(repair):
    """Install reckon skills into supported runtime skill directories.

    Copies each canonical skill into Claude, Codex, and shared agent dirs,
    preserving existing files that are identical and overwriting stale ones.
    Reports copied directories in an otherwise linked set without replacing
    them unless ``--repair`` is requested.
    """
    skills_src = _skills_source()
    skills = [
        path
        for path in sorted(skills_src.iterdir())
        if path.is_dir() and (path / "SKILL.md").is_file()
    ]
    destinations = [
        Path.home() / ".claude" / "skills",
        Path.home() / ".codex" / "skills",
        Path.home() / ".agents" / "skills",
    ]
    skipped = 0
    updated = 0

    for skills_dst in destinations:
        skills_dst.mkdir(parents=True, exist_ok=True)
        for copied, expected in _copied_where_linked(skills, skills_dst):
            click.echo(
                f"  drift    {copied.parent.parent.name}/{copied.name}: "
                f"copied directory; expected symlink → {expected}"
            )
            if repair:
                shutil.rmtree(copied)
                copied.symlink_to(expected, target_is_directory=True)
                click.echo(
                    f"  repaired {copied.parent.parent.name}/{copied.name} → {expected}"
                )
            else:
                click.echo("           run: reckon install-skills --repair")
        for skill_dir in skills:
            dst_dir = skills_dst / skill_dir.name
            dst_dir.mkdir(parents=True, exist_ok=True)
            for src_file in sorted(skill_dir.rglob("*")):
                if not src_file.is_file():
                    continue
                rel = src_file.relative_to(skill_dir)
                if "__pycache__" in rel.parts or src_file.suffix in {".pyc", ".pyo"}:
                    continue
                dst_file = dst_dir / rel
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                src_bytes = src_file.read_bytes()
                if dst_file.exists() and dst_file.read_bytes() == src_bytes:
                    skipped += 1
                else:
                    dst_file.write_bytes(src_bytes)
                    updated += 1
                    click.echo(
                        f"  updated  {skills_dst.parent.name}/{skill_dir.name}/{rel}"
                    )

    click.echo(
        f"\nDone. {updated} file{'s' if updated != 1 else ''} updated, {skipped} unchanged."
    )
    if updated == 0 and skipped == 0:
        click.echo("(No skills found in the reckon install's skills/ directory.)")
