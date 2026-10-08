import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import click

from reckon import pages
from reckon._store import _config_home
from reckon.cli_entry import (
    CREW_HOST_MISSING,
    CREW_HOST_PLUGIN_NAME,
    CREW_HOST_VALID,
    _asset_root,
    _claude_plugin_validate,
    _configure_crew_guards,
    _copied_where_linked,
    _copy_asset_directory,
    _crew_state_exists,
    _main_checkout,
    _merge_records_by_id,
    _personal_skills_dir,
    _project_docs_root,
    _reckon_checkout,
    _skills_source,
    _sync_crew_host_plugin,
    crew_host_link_state,
    main,
)
from reckon.crew_dispatch_commands import _emit
from reckon.project_setup_commands import fleet_node_group


@fleet_node_group.command(name="migrate")
@click.option(
    "--dry-run", is_flag=True, help="Print the next step without changing state."
)
@click.option("--session", help="Select one cutover or rehearsal session.")
@click.option(
    "--rehearse", is_flag=True, help="Rehearse one probe session on this node."
)
@click.option(
    "--confirm", is_flag=True, help="Confirm cancellation at the retire step."
)
def fleet_node_migrate(
    dry_run: bool, session: str | None, rehearse: bool, confirm: bool
) -> None:
    """Advance the fleet move by one recorded checkpoint."""
    from reckon.crew.fleet_migrate import MigrationError, migrate

    try:
        click.echo(
            migrate(
                dry_run=dry_run, session=session, rehearse=rehearse, confirm=confirm
            )
        )
    except MigrationError as exc:
        raise click.ClickException(str(exc)) from exc



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
        click.echo(f"  code:    {_served_code_line(module.unit_path())}")

    _service_call(action)



def _served_code_line(unit_file: Path) -> str:
    """Ask the server on this host whether it still runs the code on disk.

    The server owns the verdict (GET /_server); this reads it from the port the
    unit serves on, and says plainly when nothing answers there.
    """

    import json as json_module
    from urllib.request import urlopen

    port = 8765
    for line in unit_file.read_text().splitlines():
        argv = line.removeprefix("ExecStart=").split()
        if line.startswith("ExecStart=") and "--port" in argv[:-1]:
            value = argv[argv.index("--port") + 1]
            port = int(value) if value.isdigit() else port
    try:
        with urlopen(f"http://127.0.0.1:{port}/_server", timeout=5) as response:
            report = json_module.loads(response.read()).get("code")
    except (OSError, ValueError):
        return f"unknown — nothing answered on 127.0.0.1:{port}"
    if not isinstance(report, dict):
        return "unknown — the server does not report the code it runs"
    if report.get("stale"):
        return f"stale — {report.get('summary') or 'the code on disk has changed'}"
    return "current"



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
@click.option(
    "--claude-settings",
    default=None,
    type=click.Path(path_type=Path),
    help="Harness settings path (defaults to ~/.claude/settings.json).",
)
@click.option(
    "--remove-native-agent-guard",
    is_flag=True,
    default=False,
    help="Remove reckon's native background-agent guard from harness settings.",
)
@click.option(
    "--include-git-guard",
    is_flag=True,
    default=False,
    help="Also bind the worker git guard, which refuses a crew run's mutating git "
    "against another checkout.",
)
def sync(
    docs_path,
    project,
    mounts_file,
    state_root,
    generate_ci,
    claude_settings,
    remove_native_agent_guard,
    include_git_guard,
):
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
        if not src.is_file():
            continue
        dest = shared_dest / fname
        # Syncing the canonical checkout's own docs dir makes the asset root and
        # the destination one path, where a copy raises instead of no-opping.
        # The file is by definition already current, so report and move on.
        if src.resolve() == dest.resolve():
            click.echo(f"  canonical _shared/{fname} — already in place")
            continue
        shutil.copy2(src, dest)
        click.echo(f"  copied _shared/{fname}")

    # ── Write canonical index.html (SPA entry point) ──────────────────────
    index_html = docs_dir / "index.html"
    is_spa = index_html.is_file() and (
        "_shared/" in index_html.read_text() or "/_shared/" in index_html.read_text()
    )
    is_first_run = not index_html.exists()
    source_index = asset_root / "index.html"
    # The canonical checkout's index.html is the template every project renders
    # from. Writing a rendered copy back over it would bake one project's name
    # and title into the source that all the others inherit.
    is_own_template = index_html.exists() and index_html.resolve() == (
        source_index.resolve() if source_index.exists() else None
    )
    if is_own_template:
        click.echo("  canonical index.html — template left as authored")
    elif is_first_run or is_spa:
        from reckon.serve import _render_spa_html

        template = _render_spa_html(
            proj_name,
            index_path=source_index,
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

    settings_path = claude_settings or Path.home() / ".claude" / "settings.json"
    if remove_native_agent_guard or _crew_state_exists(docs_dir, proj_name):
        changed = _configure_crew_guards(
            settings_path,
            remove=remove_native_agent_guard,
            include_git_guard=include_git_guard,
        )
        action = "removed" if remove_native_agent_guard else "installed"
        state = action if changed else f"already {action}"
        click.echo(f"  crew harness guards {state}: {settings_path.expanduser()}")
    else:
        click.echo("  skipped native-agent guard — project has no crew state")

    # ── Session-host plugin link (user-level) ───────────────────────────────
    _sync_crew_host_plugin()

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



def _project_environment_drift() -> tuple[Path | None, list[str]]:
    """Return the source checkout and the changes ``uv sync`` would make to it.

    The MCP server is registered as ``uv run --project <checkout> reckon mcp``,
    and ``uv run`` syncs before it launches. A checkout whose environment has
    drifted from its lockfile therefore spends that sync inside the client's
    connect timeout, so the server reads as unreachable rather than stale.
    Returns ``(None, [])`` when reckon is not an editable checkout or uv is
    unavailable, since there is then no project environment to drift.
    """
    checkout = Path(__file__).resolve().parent.parent
    if not (checkout / "pyproject.toml").is_file() or shutil.which("uv") is None:
        return None, []
    try:
        proc = subprocess.run(
            ["uv", "sync", "--dry-run", "--project", str(checkout)],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return checkout, [f"uv sync --dry-run did not complete: {exc}"]
    output = (proc.stdout + proc.stderr).splitlines()
    if proc.returncode != 0:
        return checkout, output[-3:] or [f"uv sync --dry-run exited {proc.returncode}"]
    changes = [
        line.strip()
        for line in output
        if line.lstrip().startswith(("Would install", "Would uninstall", "+ ", "- "))
    ]
    return checkout, changes


def _iso_instant(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def _echo_mcp_census(report: dict) -> None:
    """Print one census report: per server, connections and failures by cause."""

    for server, row in report["servers"].items():
        click.echo(
            f"  {server}: {row['connections']} connections, "
            f"{row['failed']} failed, {row['succeeded']} clean"
        )
        for cause in row["causes"]:
            kinds = cause["directory_kind"] or "unknown"
            if cause["repository"]:
                kinds = f"{server} {kinds} of {cause['repository']}"
            click.echo(
                f"    {cause['cause']}  {cause['count']}  "
                f"newest {cause['newest']}  in {kinds}"
            )
            if cause.get("first_line"):
                click.echo(f"       {cause['first_line'][:120]}")
    storage = report.get("storage_slow") or {}
    if storage:
        summary = ", ".join(
            f"{tool} {count}" for tool, count in sorted(storage.items())
        )
        click.echo(f"  storage-slow results: {summary}")
    else:
        click.echo("  storage-slow results: none")


@main.command()
def doctor():
    """Verify reckon installation health.

    Checks:
    - Skills installed in the personal skills directory (``~/.claude/skills``
      unless ``RECKON_CLAUDE_SKILLS_DIR`` names another)
    - mounts.json reachable (default: ~/docs-server/mounts.json)
    - Every mounted project directory exists
    - Reckon MCP registration present in Claude Code, Claude Desktop or Codex config
    - The source checkout's environment matches its lockfile, so the MCP
      server's ``uv run`` launch does not sync inside the connect timeout

    Prints a green checkmark on pass or a named fix suggestion on fail.
    """
    import sys

    ok = True
    skills = sorted(
        path.name
        for path in _skills_source().iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    )
    skills_dir = _personal_skills_dir()

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

    # ── Session-host plugin check ────────────────────────────────────────────
    click.echo("\nSession host plugin")
    plugin_checkout = _main_checkout(_reckon_checkout())
    link = crew_host_link_state(_personal_skills_dir(), plugin_checkout)
    if link.state == CREW_HOST_VALID:
        message = _claude_plugin_validate(link.dest)
        if message:
            click.echo(f"  ✗  {CREW_HOST_PLUGIN_NAME} invalid: {message}")
            click.echo(f"       run: claude plugin validate {link.dest}")
            ok = False
        elif message is None:
            click.echo(f"  ✓  {CREW_HOST_PLUGIN_NAME} → {link.target}")
            click.echo("       claude not on PATH — plugin validate skipped")
        else:
            click.echo(f"  ✓  {CREW_HOST_PLUGIN_NAME} → {link.target}")
    elif link.state == CREW_HOST_MISSING:
        # A link that was never made is the ordinary state of a checkout that
        # has not run sync yet, so it is reported with its fix but does not
        # fail the run; the broken states below resolve to nothing or to the
        # wrong tree, which no sync will repair on its own.
        click.echo(f"  ·  {CREW_HOST_PLUGIN_NAME} not linked  →  run: reckon sync")
    else:
        click.echo(f"  ✗  {CREW_HOST_PLUGIN_NAME} {link.state}: {link.detail}")
        click.echo("       run: reckon sync")
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
        Path.home() / ".claude.json",
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

    # ── MCP connections check ────────────────────────────────────────────────
    click.echo("\nMCP connections")
    from reckon.mcp_census import census

    now = datetime.now(UTC)
    report = census(
        window_start=_iso_instant(now.timestamp() - 7 * 24 * 3600),
        window_end=_iso_instant(now.timestamp()),
    )
    _echo_mcp_census(report)

    # ── Environment check ────────────────────────────────────────────────────
    checkout, drift = _project_environment_drift()
    if checkout is not None:
        click.echo("\nEnvironment")
        if drift:
            click.echo(f"  ✗  {checkout} environment differs from its lockfile:")
            for line in drift[:10]:
                click.echo(f"       {line}")
            click.echo(
                f"       run: uv sync --project {checkout}  "
                "(the MCP launch would otherwise sync inside the connect timeout)"
            )
            ok = False
        else:
            click.echo(f"  ✓  {checkout} environment matches its lockfile")

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
@click.option(
    "--checks/--no-checks",
    default=True,
    show_default=True,
    help="Also list documents that fail the render contract.",
)
def audit(project, checks):
    """Report stale lifecycle state across mounted reckon projects.

    Flags:
      - STALE: active plans older than 30 days with impl < 1.0
      - MISSING_IMPL: shipped/done plans with missing or zero impl
      - STALE_RCA: research docs older than 60 days that are not done/archived

    Then lists every live document that fails the render contract — the check
    ``reckon audit-doc`` runs — using stored verdicts and checking only the
    documents that changed since.

    Exits 1 when any MISSING_IMPL row is found (CI-friendly); a render-contract
    failure is listed but does not change the exit code.
    """
    import sys

    from reckon.doccheck import audit_lifecycle

    try:
        findings = audit_lifecycle(project=project)
    except ValueError as e:
        raise click.ClickException(str(e)) from e

    if not findings:
        click.echo("No lifecycle hygiene findings.")
    else:
        _echo_lifecycle_findings(findings)
    if checks:
        _echo_render_contract_failures(project)
    if any(item.flag == "MISSING_IMPL" for item in findings):
        sys.exit(1)



def _echo_render_contract_failures(project: str | None) -> None:
    """List each live document that fails the render contract, per mount."""

    from reckon import compliance
    from reckon.doccheck import _load_mounts

    mounts = _load_mounts()
    names = [project] if project else sorted(mounts)
    rows = []
    for name in names:
        docs_dir = mounts.get(name)
        if docs_dir is None or not Path(docs_dir).is_dir():
            continue
        compliance.refresh(Path(docs_dir), name)
        for document in compliance.project_checks(Path(docs_dir), name)["documents"]:
            first = next(
                (f for f in document["findings"] if f["severity"] == "error"), None
            )
            detail = f"{first['code']}: {first['message']}" if first else ""
            if len(detail) > 90:
                detail = detail[:89] + "…"
            rows.append((name, document["path"], str(document["errors"]), detail))
    click.echo("")
    if not rows:
        click.echo("No render-contract failures.")
        return
    click.echo(
        "Render-contract failures (run reckon audit-doc <path> for every finding):"
    )
    headers = ("project", "document", "errors", "first error")
    widths = [
        max(len(header), *(len(row[idx]) for row in rows))
        for idx, header in enumerate(headers)
    ]
    fmt = "  ".join(f"{{:<{width}}}" for width in widths)
    click.echo(fmt.format(*headers))
    click.echo(fmt.format(*("-" * width for width in widths)))
    for row in rows:
        click.echo(fmt.format(*row))



def _echo_lifecycle_findings(findings) -> None:
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



@main.group(name="hooks")
def hooks():
    """Install reckon's harness hooks into a settings file."""



@hooks.command(name="install")
@click.option(
    "--scope",
    type=click.Choice(["user"], case_sensitive=False),
    default="user",
    show_default=True,
    help="The settings scope the hooks are installed into.",
)
@click.option(
    "--write",
    is_flag=True,
    help="Merge the fragment into the settings file; without it only the fragment prints.",
)
@click.option(
    "--settings",
    "settings_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Settings file to install into (default: the scope's own file).",
)
def hooks_install(scope, write, settings_path):
    """Print the hook settings fragment, merging it only with --write.

    The fragment binds the coordinator-obligations hook in both of its modes and
    the worker stop hook. Without ``--write`` this prints the fragment and opens
    no file; with it, the fragment is merged into the target settings file,
    preserving every existing key and hook group. A target that already carries
    the whole fragment is a duplicate install, and the command refuses rather
    than reporting a silent no-op.

    The fragment names the hook scripts of the checkout this command runs from,
    and the checkout it resolved is printed to stderr so the caller can see which
    tree the installed hooks will read — an install run from a worker worktree
    binds commands to a tree that is reclaimed when the worktree is removed.
    """
    from reckon.hooks import install as installer
    from reckon.hooks.install import HookInstallError

    scopes = {"user": installer.user_settings_path}
    resolver = scopes.get(scope.lower())
    if resolver is None:
        raise click.UsageError(f"unsupported scope: {scope}")
    target = Path(settings_path) if settings_path is not None else resolver()

    checkout = Path(installer.__file__).resolve().parents[2]
    click.echo(f"hook fragment names scripts under {checkout}", err=True)

    try:
        result = installer.install_hook_settings(target, write=write)
    except HookInstallError as exc:
        raise click.ClickException(str(exc)) from exc
    if write and not result.added:
        raise click.ClickException(
            "hook entries already installed: " + ", ".join(result.skipped)
        )

