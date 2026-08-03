import json
import shutil
from datetime import datetime, UTC
from pathlib import Path

import click

from reckon._store import _config_home


def _asset_root() -> Path:
    """Resolve the canonical frontend assets from an install or source tree."""
    package_dir = Path(__file__).resolve().parent
    candidates = (package_dir / "_assets", package_dir.parent / "docs")
    required = {
        "ui": ("shell.jsx", "state-loader.js"),
        "_shared": ("foundation.css", "dashboard.css", "state.js"),
    }
    for root in candidates:
        if all(
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


@click.group()
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
    help="Write .github/workflows/reckon-pages.yml.",
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

    proj_name = project or docs_dir.parent.name
    asset_root = _asset_root()

    click.echo(f"Syncing {proj_name} → {docs_dir}")

    # ── Copy shared CSS + state.js ─────────────────────────────────────────
    shared_src = asset_root / "_shared"
    shared_dest = docs_dir / "_shared"
    shared_dest.mkdir(parents=True, exist_ok=True)
    for fname in ("foundation.css", "dashboard.css"):
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
        template = f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"docs-project\" content=\"{proj_name}\">
  <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
  <title>reckon · {proj_name}</title>
  <link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">
  <link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>
  <link href=\"https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500&display=swap\" rel=\"stylesheet\">
  <link rel=\"stylesheet\" href=\"/_shared/foundation.css\">
  <link rel=\"stylesheet\" href=\"/_shared/dashboard.css\">
  <link rel=\"stylesheet\" href=\"/_ui/project.css\">
  <link rel=\"stylesheet\" href=\"/_ui/styles-base.css\">
  <link rel=\"stylesheet\" href=\"/_ui/styles.css\">
  <script src=\"https://unpkg.com/react@18.3.1/umd/react.development.js\" integrity=\"sha384-hD6/rw4ppMLGNu3tX5cjIb+uRZ7UkRJ6BPkLpg4hAu/6onKUg4lLsHAs9EBPT82L\" crossorigin=\"anonymous\"></script>
  <script src=\"https://unpkg.com/react-dom@18.3.1/umd/react-dom.development.js\" integrity=\"sha384-u6aeetuaXnQ38mYT8rp6sbXaQe3NL9t+IBXmnYxwkUI2Hw4bsp2Wvmx4yRQF1uAm\" crossorigin=\"anonymous\"></script>
  <script src=\"https://unpkg.com/@babel/standalone@7.29.0/babel.min.js\" integrity=\"sha384-m08KidiNqLdpJqLq95G/LEi8Qvjl/xUYll3QILypMoQ65QorJ9Lvtp2RXYGBFj1y\" crossorigin=\"anonymous\"></script>
</head>
<body>
  <div id=\"root\"></div>
  <script src=\"/_ui/state-loader.js\"></script>
  <script type=\"text/babel\" src=\"/_ui/ui.jsx\"></script>
  <script type=\"text/babel\" src=\"/_ui/bits.jsx\"></script>
  <script type=\"text/babel\" src=\"/_ui/decision.jsx\"></script>
  <script type=\"text/babel\" src=\"/_ui/cockpit.jsx\"></script>
  <script type=\"text/babel\" src=\"/_ui/plan.jsx\"></script>
  <script type=\"text/babel\" src=\"/_ui/sprint.jsx\"></script>
  <script type=\"text/babel\" src=\"/_ui/graph.jsx\"></script>
  <script type=\"text/babel\" src=\"/_ui/shell.jsx\"></script>
</body>
</html>
"""
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

    # ── Seed project.json (sprint/milestone definitions) ──────────────────
    proj_json = state_dir / "project.json"
    if not proj_json.exists():
        seed = {
            "updated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
            "project": proj_name,
            "doc": "project",
            "data": {"sprints": [], "milestones": [], "blockers": []},
        }
        proj_json.write_text(json.dumps(seed, indent=2) + "\n")
        click.echo(f"  seeded state/{proj_name}/project.json")

    # ── Seed legacy index state (distributed projects remain immutable) ───────
    # Inventory is discovered live by the server on every request — writing it
    # here would create stale data that the MCP tools read instead of the live view.
    index_json = state_dir / "index.json"
    from reckon.project_state import project_state_mode

    if project_state_mode(docs_dir).format == "distributed":
        click.echo("  preserved frozen index.json (distributed project state)")
    else:
        from reckon.serve import discover_plans

        discovered = discover_plans(docs_dir, proj_name, state_dir.parent)
        idx_data: dict = {}
        if index_json.is_file():
            try:
                env = json.loads(index_json.read_text())
                idx_data = env.get("data", {})
            except json.JSONDecodeError:
                pass

        if not idx_data.get("sprints") and discovered.get("sprints"):
            idx_data["sprints"] = discovered["sprints"]
        if not idx_data.get("milestones") and discovered.get("milestones"):
            idx_data["milestones"] = discovered["milestones"]
        if not idx_data.get("active_sprint_id"):
            active = next(
                (
                    s
                    for s in (idx_data.get("sprints") or [])
                    if s.get("status") == "active"
                ),
                None,
            )
            if active:
                idx_data["active_sprint_id"] = active["id"]

        idx_data.pop("inventory", None)
        idx_data["_version"] = (idx_data.get("_version") or 0) + 1
        envelope = {
            "updated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
            "project": proj_name,
            "doc": "index",
            "data": idx_data,
        }
        index_json.write_text(json.dumps(envelope, indent=2) + "\n")
        n_sprints = len(idx_data.get("sprints") or [])
        n_miles = len(idx_data.get("milestones") or [])
        click.echo(
            f"  seeded index.json (sprints={n_sprints} milestones={n_miles}) "
            "— inventory discovered live"
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
        workflows_dir = repo_root / ".github" / "workflows"
        workflows_dir.mkdir(parents=True, exist_ok=True)
        ci_yml = workflows_dir / "reckon-pages.yml"
        ci_yml.write_text(_CI_WORKFLOW_TEMPLATE.format(docs_path=docs_path))
        click.echo(f"  wrote {ci_yml.relative_to(repo_root)}")

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

    Copies all JSX + CSS from the reckon install into docs/_ui/ and docs/_shared/,
    generates an index.html with relative asset paths (compatible with GitHub Pages),
    and writes a complete index.json with live-discovered inventory + sprints/milestones
    so the SPA works without a running reckon server.

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

    # ── Copy shared CSS + state.js ─────────────────────────────────────────
    shared_src = asset_root / "_shared"
    shared_dest = docs_dir / "_shared"
    copied_shared = _copy_asset_directory(shared_src, shared_dest)
    click.echo(f"  copied _shared/ ({copied_shared} files)")

    # ── Generate index.html with RELATIVE paths ────────────────────────────
    index_html = docs_dir / "index.html"
    index_html.write_text(_BUILD_INDEX_TEMPLATE.format(project=proj_name))
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


@main.command(name="migrate-project-state")
@click.argument("docs_path", type=click.Path(path_type=Path))
@click.option(
    "--project", default=None, help="Project key (defaults to docs parent dir name)."
)
def migrate_project_state_command(docs_path, project):
    """Split a legacy project index into independently versioned resources."""
    from reckon.project_state import ProjectStateError, migrate_project_state

    docs_dir = docs_path.expanduser().resolve()
    if not docs_dir.is_dir():
        raise click.ClickException(f"docs path not found: {docs_dir}")
    proj_name = project or docs_dir.parent.name
    try:
        result = migrate_project_state(docs_dir, proj_name)
    except ProjectStateError as exc:
        raise click.ClickException(str(exc)) from exc
    verb = "migrated" if result.get("changed") else "verified"
    click.echo(
        f"{verb} project state: {len(result.get('resources', []))} resources; "
        f"source {result.get('source_sha256', '')[:12]}; "
        f"parity {result.get('parity_sha256', '')[:12]}"
    )
    click.echo(f"marker: {docs_dir / '.reckon/project-state-migration.json'}")


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


@main.command(name="migrate-fleet")
@click.option(
    "--mounts",
    "mounts_path",
    default=None,
    type=click.Path(path_type=Path),
    help="Authoritative mounts registry (defaults to Reckon config resolution).",
)
@click.option(
    "--output-dir",
    default=None,
    type=click.Path(path_type=Path),
    help="Run ledger/snapshot directory (defaults below Reckon config home).",
)
@click.option(
    "--run-id",
    default=None,
    help="Stable safe identifier for an idempotent run.",
)
@click.option(
    "--apply-project",
    "apply_projects",
    multiple=True,
    help="Project explicitly selected for mutation; repeat for a reviewed wave.",
)
def migrate_fleet(mounts_path, output_dir, run_id, apply_projects):
    """Snapshot the active registry and migrate an explicitly selected wave."""
    from reckon.fleet_migration import FleetMigrationError, run_fleet_migration

    try:
        ledger = run_fleet_migration(
            mounts_path=mounts_path,
            output_dir=output_dir,
            run_id=run_id,
            apply_projects=apply_projects,
        )
    except FleetMigrationError as exc:
        raise click.ClickException(str(exc)) from exc
    rows = ledger["repositories"]
    for row in rows:
        detail = row.get("error") or row.get("required_action") or ""
        click.echo(f"{row['project']}: {row['state']} {detail}".rstrip())
    click.echo(f"ledger: {ledger['ledger_path']}")
    click.echo(
        f"terminal: {sum(row['state'] in {'deferred', 'verified', 'rolled-back'} for row in rows)}/{len(rows)}"
    )


@main.command(name="migration-record")
@click.argument("ledger_path", type=click.Path(path_type=Path))
@click.argument("project")
@click.argument("commit")
@click.argument("push_ref")
def migration_record(ledger_path, project, commit, push_ref):
    """Attach commit and push evidence to a verified fleet-ledger row."""
    from reckon.fleet_migration import (
        FleetMigrationError,
        record_repository_commit,
    )

    try:
        row = record_repository_commit(
            ledger_path,
            project,
            commit,
            push_ref,
        )
    except FleetMigrationError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"recorded {row['project']}: {row['output_commit']} → {row['push_ref']}")


@main.command(name="migration-inventory")
@click.argument("ledger_path", type=click.Path(path_type=Path))
def migration_inventory(ledger_path):
    """Backfill before/after resource counts from snapshots and verified trees."""
    from reckon.fleet_migration import (
        FleetMigrationError,
        enrich_ledger_inventories,
    )

    try:
        ledger = enrich_ledger_inventories(ledger_path)
    except FleetMigrationError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"inventoried {len(ledger.get('repositories', []))} repository row(s)")


@main.command(name="migration-rollback")
@click.argument("snapshot", type=click.Path(path_type=Path))
@click.argument("docs_path", type=click.Path(path_type=Path))
@click.option(
    "--path",
    "changed_paths",
    multiple=True,
    required=True,
    help="Exact migration path to restore/remove; repeat as needed.",
)
def migration_rollback(snapshot, docs_path, changed_paths):
    """Restore exact recorded paths from one content-bearing snapshot."""
    from reckon.fleet_migration import FleetMigrationError, rollback_repository

    try:
        result = rollback_repository(snapshot, docs_path, changed_paths)
    except FleetMigrationError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"restored {len(result['restored'])} path(s)")


@main.command()
def doctor():
    """Verify reckon installation health.

    Checks:
    - Skills installed at ~/.claude/skills/reckon-*/
    - mounts.json reachable (default: ~/docs-server/mounts.json)
    - Every mounted project directory exists
    - MCP config present at ~/.claude/claude_desktop_config.json or ~/.config/claude/claude_desktop_config.json

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
    mcp_candidates = [
        Path.home() / ".claude" / "claude_desktop_config.json",
        Path.home() / ".config" / "claude" / "claude_desktop_config.json",
    ]
    mcp_found = None
    for candidate in mcp_candidates:
        if candidate.is_file():
            mcp_found = candidate
            break
    if mcp_found is None:
        click.echo(
            "  ✗  claude_desktop_config.json not found — MCP server may not be registered"
        )
        click.echo("       see: https://docs.reckon.dev/mcp")
        ok = False
    else:
        try:
            cfg = json.loads(mcp_found.read_text())
            servers = cfg.get("mcpServers", {})
            if "reckon" in servers:
                click.echo(f"  ✓  MCP server 'reckon' registered in {mcp_found.name}")
            else:
                click.echo(f"  ✗  MCP server 'reckon' not in {mcp_found.name}")
                click.echo("       add it with:  reckon mcp --install")
                ok = False
        except (json.JSONDecodeError, OSError) as e:
            click.echo(f"  ✗  {mcp_found.name} unreadable: {e}")
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


def _print_roadmap_report(report: dict) -> None:
    project = report.get("project", "")
    completion = report.get("completion", {})
    click.echo(
        f"{project}: {completion.get('lifecycle_completion_pct', 0):.1f}% lifecycle, "
        f"{completion.get('implementation_pct', 0):.1f}% implementation; "
        f"{len(report.get('ready_now', []))} ready, "
        f"{len(report.get('blocked', []))} blocked"
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
            f"{portfolio.get('ready', 0)} ready, {portfolio.get('blocked', 0)} blocked"
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
def install_skills():
    """Install reckon skills into supported runtime skill directories.

    Copies each canonical skill into Claude, Codex, and shared agent dirs,
    preserving existing files that are identical and overwriting stale ones.
    Reports: skipped (unchanged) vs updated (changed or new).
    """
    skills_src = _skills_source()
    destinations = [
        Path.home() / ".claude" / "skills",
        Path.home() / ".codex" / "skills",
        Path.home() / ".agents" / "skills",
    ]
    skipped = 0
    updated = 0

    for skills_dst in destinations:
        skills_dst.mkdir(parents=True, exist_ok=True)
        for skill_dir in sorted(skills_src.iterdir()):
            if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").is_file():
                continue
            dst_dir = skills_dst / skill_dir.name
            dst_dir.mkdir(parents=True, exist_ok=True)
            for src_file in sorted(skill_dir.rglob("*")):
                if not src_file.is_file():
                    continue
                rel = src_file.relative_to(skill_dir)
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


# ── Static build index.html template (relative asset paths) ────────────────

_BUILD_INDEX_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="docs-project" content="{project}">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>reckon · {project}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="_shared/foundation.css">
  <link rel="stylesheet" href="_shared/dashboard.css">
  <link rel="stylesheet" href="_ui/project.css">
  <link rel="stylesheet" href="_ui/styles-base.css">
  <link rel="stylesheet" href="_ui/styles.css">
  <script src="https://unpkg.com/react@18.3.1/umd/react.development.js" integrity="sha384-hD6/rw4ppMLGNu3tX5cjIb+uRZ7UkRJ6BPkLpg4hAu/6onKUg4lLsHAs9EBPT82L" crossorigin="anonymous"></script>
  <script src="https://unpkg.com/react-dom@18.3.1/umd/react-dom.development.js" integrity="sha384-u6aeetuaXnQ38mYT8rp6sbXaQe3NL9t+IBXmnYxwkUI2Hw4bsp2Wvmx4yRQF1uAm" crossorigin="anonymous"></script>
  <script src="https://unpkg.com/@babel/standalone@7.29.0/babel.min.js" integrity="sha384-m08KidiNqLdpJqLq95G/LEi8Qvjl/xUYll3QILypMoQ65QorJ9Lvtp2RXYGBFj1y" crossorigin="anonymous"></script>
</head>
<body>
  <div id="root"></div>
  <script src="_ui/state-loader.js"></script>
  <script type="text/babel" src="_ui/glyphs.jsx"></script>
  <script type="text/babel" src="_ui/_shared.jsx"></script>
  <script src="_ui/prompts.js"></script>
  <script type="text/babel" src="_ui/ui.jsx"></script>
  <script type="text/babel" src="_ui/bits.jsx"></script>
  <script type="text/babel" src="_ui/decision.jsx"></script>
  <script type="text/babel" src="_ui/cockpit.jsx"></script>
  <script type="text/babel" src="_ui/plan.jsx"></script>
  <script type="text/babel" src="_ui/sprint.jsx"></script>
  <script type="text/babel" src="_ui/graph.jsx"></script>
  <script type="text/babel" src="_ui/shell.jsx"></script>
</body>
</html>
"""
