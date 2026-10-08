import json
import shutil
import sys
from pathlib import Path

import click

from reckon import pages
from reckon._store import _state_root
from reckon.cli_entry import _project_docs_root, main
from reckon.crew_dispatch_commands import _emit


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



@main.command(name="fleet")
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def fleet(pretty):
    """Read the compact cross-project fleet rollup."""

    from reckon import fleet_index
    from reckon.project_state import ProjectStateError
    from reckon.serve import load_mounts

    mounts = load_mounts()
    if not mounts:
        raise click.ClickException(
            "no projects are mounted; run `reckon sync` in a project first"
        )
    try:
        rows = fleet_index.collect_project_rows(mounts, state_root=_state_root())
    except (OSError, ProjectStateError, ValueError) as exc:
        raise click.ClickException(f"cannot read fleet rollup: {exc}") from exc
    _emit({"ok": True, "view": "fleet", "projects": rows}, pretty)



@main.group(name="fleet-node")
def fleet_node_group():
    """Hold, read and place work on the fleet node's whole-node allocation."""



@fleet_node_group.command(name="hold")
@click.option(
    "--submit",
    is_flag=True,
    help="Submit the allocation instead of printing its SLURM script.",
)
def fleet_node_hold(submit):
    """Print, or explicitly submit, the persistent whole-node allocation.

    ``cx`` runs this with --submit when it finds no fleet running; the line it
    prints names the job id ``cx`` records.
    """
    from reckon.crew import fleet_node

    log_path = fleet_node.batch_log_path()
    script = fleet_node.generate_hold_script(fleet_node.fleet_size(), log_path=log_path)
    if not submit:
        click.echo(script, nl=False)
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        job_id = fleet_node.submit(script)
    except fleet_node.FleetNodeError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Submitted fleet allocation job {job_id}.")



@fleet_node_group.command(name="status")
def fleet_node_status():
    """Report the held allocation, its node and its remaining lifetime.

    The allocation is found by its comment token under the account it is
    charged to, and the node's own scheduler state is read too: a draining node
    ends an allocation that has no wall clock, and is warned about on its own.
    """
    from reckon.crew import fleet_node

    try:
        jobs = fleet_node.query_jobs(fleet_node.fleet_size().account)
    except fleet_node.FleetNodeError as exc:
        raise click.ClickException(str(exc)) from exc
    allocations = fleet_node.find_allocations(jobs)
    if not allocations:
        click.echo(
            "No fleet allocation is held; the interactive fleet has no "
            "whole-node allocation in the queue."
        )
        return
    recorded = fleet_node.recorded_job_id()
    for allocation in allocations:
        state = fleet_node.node_state(allocation.get("node", ""))
        for line in fleet_node.describe_allocation(allocation, state_of_node=state):
            click.echo(line)
        if allocation.get("jobid", "").strip() == recorded:
            click.echo("  hosts      the fleet sessions (named by the fleet record)")
    if len(allocations) > 1:
        click.echo(
            f"{len(allocations)} fleet allocations are held; "
            + (
                f"only {recorded} hosts the sessions."
                if recorded in {job.get("jobid", "").strip() for job in allocations}
                else "the fleet record names none of them."
            )
        )



@fleet_node_group.command(
    name="place",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.argument("command", nargs=-1, required=True, type=click.UNPROCESSED)
def fleet_node_place(command):
    """Run a command as a scheduler step inside the held fleet allocation.

    The allocation is resolved from the queue at launch, so no job id is written
    into configuration. Holding no allocation is a refusal rather than a launch
    on the login node, because a command meant to be placed that silently is not
    runs under the ceiling the placement exists to escape.
    """
    from reckon.crew import fleet_node

    try:
        jobs = fleet_node.query_jobs(fleet_node.fleet_size().account)
    except fleet_node.FleetNodeError as exc:
        raise click.ClickException(str(exc)) from exc
    allocation = fleet_node.find_allocation(
        jobs, preferred=fleet_node.recorded_job_id()
    )
    if allocation is None:
        raise click.ClickException(
            "No fleet allocation is held, so there is nowhere to place "
            f"{' '.join(command)!r}; the login node is not the fleet's host. "
            "Hold one with `reckon fleet-node hold --submit`."
        )
    result = fleet_node.subprocess.run(
        fleet_node.placement_argv(allocation, command), check=False
    )
    if result.returncode != 0:
        raise SystemExit(result.returncode)



@main.command(name="paste")
@click.option(
    "--no-fleet",
    is_flag=True,
    help="Keep an image on this host; do not copy it to the live fleet node.",
)
def paste_command(no_fleet):
    """Paste the terminal client's clipboard: an image's path, or the text.

    An image is written to /tmp here and, when a fleet allocation is running,
    at the same path on its node, so the printed path is valid in a fleet
    session too. Inside a SLURM job, such as a fleet pane, the clipboard is
    reached through the job's login node. `pi` is this command.
    """
    from reckon.paste import paste

    sys.exit(paste(fleet=not no_fleet))



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
@click.option(
    "--project",
    default=None,
    help="Narrow the published rows to configurations fed by this project.",
)
def capabilities_command(rebuild, pretty, project):
    """Publish one truthful row per worker configuration from the cache.

    Rows carry the competence horizon, speed mean and median, and sample
    size as numbers, plus an explicit horizon legibility state and per-row
    freshness naming both ledger versions whenever the backing cache trails
    its project's ledger.
    """

    from reckon import capabilities as capabilities_module

    if rebuild:
        capabilities_module.rebuild_capabilities()
    try:
        payload = capabilities_module.publish_capabilities(project=project)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit({"rebuilt": bool(rebuild), **payload}, pretty)



@main.command(name="probe-held-blocker")
@click.option("--project", required=True, help="Project owning the blocker.")
@click.option(
    "--checkout-path",
    default=None,
    type=click.Path(path_type=Path),
    help="Optional repository checkout root; defaults to the mounted project.",
)
@click.argument("blocker_id")
def probe_held_blocker(project, checkout_path, blocker_id):
    """Evaluate a held blocker's registered probe and report its finding."""
    from reckon.project_state import ProjectStateError, evaluate_held_blocker

    docs_dir = _project_docs_root(project, checkout_path)
    try:
        report = evaluate_held_blocker(docs_dir, project, blocker_id)
    except (ProjectStateError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    _emit(report, pretty=False)



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

