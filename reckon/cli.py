import json
import shutil
from datetime import datetime, UTC
from pathlib import Path

import click


@click.group()
def main():
    """reckon — repo-agnostic agile planning system."""


@main.command()
@click.option("--port", default=8765, show_default=True, help="Port to listen on.")
@click.option("--host", default=None, help="Bind address (default: $DOCS_SERVER_BIND or 127.0.0.1).")
@click.option("--mounts", "mounts_file", default=None, type=click.Path(path_type=Path), help="Path to mounts.json.")
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
@click.option("--project", default=None, help="Project key (defaults to docs parent dir name).")
@click.option("--mounts", "mounts_file", default=None, type=click.Path(path_type=Path), help="Path to mounts.json.")
@click.option("--state-root", default=None, type=click.Path(path_type=Path), help="State root dir.")
def sync(docs_path, project, mounts_file, state_root):
    """Register a project and copy reckon UI files into its docs directory.

    DOCS_PATH is the path to the project's docs/ directory
    (or the directory where plan HTML pages live).

    reckon copies CSS, JSX, and state-loader from its own canonical source,
    registers the project in mounts.json, and creates a state directory.
    Plans are discovered automatically from HTML <meta name="plan-*"> tags —
    no index.json authoring required.

    Run this once to set up a new project, and again after a reckon update
    to pull in the latest CSS/JSX.
    """
    docs_dir = docs_path.expanduser().resolve()
    if not docs_dir.exists():
        raise click.ClickException(f"docs path not found: {docs_dir}")

    proj_name = project or docs_dir.parent.name
    reckon_root = Path(__file__).parent.parent  # ~/Code/reckon

    click.echo(f"Syncing {proj_name} → {docs_dir}")

    # ── Copy shared CSS + state.js ─────────────────────────────────────────
    shared_src  = reckon_root / "docs" / "_shared"
    shared_dest = docs_dir / "_shared"
    shared_dest.mkdir(parents=True, exist_ok=True)
    for fname in ("foundation.css", "dashboard.css", "state.js"):
        src = shared_src / fname
        if src.is_file():
            shutil.copy2(src, shared_dest / fname)
            click.echo(f"  copied _shared/{fname}")

    # ── Copy UI components ─────────────────────────────────────────────────
    ui_src  = reckon_root / "docs" / "ui"
    ui_dest = docs_dir / "ui"
    ui_dest.mkdir(parents=True, exist_ok=True)
    ui_files = ["state-loader.js", "ui.jsx",
                "bits.jsx", "cockpit.jsx", "plan.jsx",
                "plan-decision.jsx", "plan-tokenizers.jsx",
                "shell.jsx", "sprint.jsx",
                "project.css", "styles.css"]
    for fname in ui_files:
        src = ui_src / fname
        if src.is_file():
            shutil.copy2(src, ui_dest / fname)
            click.echo(f"  copied ui/{fname}")

    # ── Drop .nojekyll (GitHub Pages) ─────────────────────────────────────
    nojekyll = docs_dir / ".nojekyll"
    if not nojekyll.exists():
        nojekyll.touch()
        click.echo("  created .nojekyll")

    # ── State directory + symlink ──────────────────────────────────────────
    ds_root = (state_root or Path.home() / "docs-server" / "state").expanduser().resolve()
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

    # ── Register in mounts.json ────────────────────────────────────────────
    mounts_path = (mounts_file or Path.home() / "docs-server" / "mounts.json").expanduser()
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

    click.echo(f"\nDone. Visit http://localhost:8765/{proj_name}/ once the server is running.")
    click.echo("Plans are auto-discovered from HTML <meta name=\"plan-status\"> tags.")
    click.echo("See reckon-sync SKILL.md for the plan HTML style guide.")
