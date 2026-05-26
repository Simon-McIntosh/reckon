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
    reckon_root = Path(__file__).parent.parent  # ~/Code/reckon

    click.echo(f"Syncing {proj_name} → {docs_dir}")

    # ── Copy shared CSS + state.js ─────────────────────────────────────────
    shared_src  = reckon_root / "docs" / "_shared"
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

    # ── Seed index.json with sprint/milestone structure (no inventory) ────────
    # Inventory is discovered live by the server on every request — writing it
    # here would create stale data that the MCP tools read instead of the live view.
    from reckon.serve import discover_plans
    discovered = discover_plans(docs_dir, proj_name, state_dir.parent)

    index_json = state_dir / "index.json"
    idx_data: dict = {}
    if index_json.is_file():
        try:
            env = json.loads(index_json.read_text())
            idx_data = env.get("data", {})
        except json.JSONDecodeError:
            pass

    # Seed sprints/milestones from project.json discovery if not in index yet
    if not idx_data.get("sprints") and discovered.get("sprints"):
        idx_data["sprints"] = discovered["sprints"]
    if not idx_data.get("milestones") and discovered.get("milestones"):
        idx_data["milestones"] = discovered["milestones"]
    if not idx_data.get("active_sprint_id"):
        active = next((s for s in (idx_data.get("sprints") or []) if s.get("status") == "active"), None)
        if active:
            idx_data["active_sprint_id"] = active["id"]

    # Purge any stale inventory — server discovers it live; MCP tools must too.
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
    click.echo(f"  seeded index.json (sprints={n_sprints} milestones={n_miles}) — inventory discovered live")

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
    click.echo("New plan pages appear live — the server discovers HTML <meta name=\"plan-*\"> tags on every request.")
    click.echo("UI assets (JSX, CSS) are served directly from the reckon install — no per-project copies needed.")
    click.echo("Re-run sync only to update shared CSS after a reckon upgrade.")
