import json
import shutil
from datetime import datetime, UTC
from pathlib import Path

import click

from reckon._store import _config_home


@click.group()
def main():
    """reckon — repo-agnostic agile planning system."""


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
    reckon_root = Path(__file__).parent.parent  # ~/Code/reckon

    click.echo(f"Syncing {proj_name} → {docs_dir}")

    # ── Copy shared CSS + state.js ─────────────────────────────────────────
    shared_src = reckon_root / "docs" / "_shared"
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
        active = next(
            (s for s in (idx_data.get("sprints") or []) if s.get("status") == "active"),
            None,
        )
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
    click.echo(
        f"  seeded index.json (sprints={n_sprints} milestones={n_miles}) — inventory discovered live"
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
      - uses: actions/setup-python@v5
        with: {{ python-version: "3.12" }}
      - run: pip install reckon
      - run: reckon build {docs_path}
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
    reckon_root = Path(__file__).parent.parent

    click.echo(f"Building static site: {proj_name} → {docs_dir}")

    # ── Copy UI assets (JSX + CSS) ─────────────────────────────────────────
    ui_src = reckon_root / "docs" / "ui"
    ui_dest = docs_dir / "_ui"
    if ui_src.is_dir():
        if ui_dest.exists() and not ui_dest.is_dir():
            raise click.ClickException(f"_ui exists but is not a directory: {ui_dest}")
        ui_dest.mkdir(parents=True, exist_ok=True)
        copied = 0
        for src_file in sorted(ui_src.iterdir()):
            if src_file.is_file():
                shutil.copy2(src_file, ui_dest / src_file.name)
                copied += 1
        click.echo(f"  copied _ui/ ({copied} files)")

    # ── Copy shared CSS + state.js ─────────────────────────────────────────
    shared_src = reckon_root / "docs" / "_shared"
    shared_dest = docs_dir / "_shared"
    if shared_src.is_dir():
        shared_dest.mkdir(parents=True, exist_ok=True)
        for src_file in sorted(shared_src.iterdir()):
            if src_file.is_file():
                shutil.copy2(src_file, shared_dest / src_file.name)
        click.echo("  copied _shared/")

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
    idx_data: dict = {}
    if index_json.is_file():
        try:
            env = json.loads(index_json.read_text())
            idx_data = dict(env.get("data", {}))
        except json.JSONDecodeError:
            pass

    idx_data["inventory"] = discovered["inventory"]
    idx_data["sprints"] = discovered["sprints"]
    idx_data["milestones"] = discovered["milestones"]
    if not idx_data.get("active_sprint_id"):
        active = next(
            (s for s in idx_data["sprints"] if s.get("status") == "active"), None
        )
        if active:
            idx_data["active_sprint_id"] = active["id"]
    idx_data["_version"] = (idx_data.get("_version") or 0) + 1

    envelope = {
        "updated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
        "project": proj_name,
        "doc": "index",
        "data": idx_data,
    }
    index_json.write_text(json.dumps(envelope, indent=2) + "\n")
    n_plans = len(idx_data["inventory"])
    n_sprints = len(idx_data["sprints"])
    click.echo(
        f"  wrote state/{proj_name}/index.json ({n_plans} plans, {n_sprints} sprints)"
    )

    click.echo(
        f"\nBuild complete. Deploy the {docs_dir.name}/ directory as a static site."
    )


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
    SKILLS = [
        "reckon-create",
        "reckon-edit",
        "reckon-ship",
        "reckon-status",
        "reckon-sync",
    ]
    skills_dir = Path.home() / ".claude" / "skills"

    click.echo("reckon doctor\n")

    # ── Skills check ────────────────────────────────────────────────────────
    click.echo("Skills")
    for skill in SKILLS:
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


@main.command(name="audit-doc")
@click.argument("paths", nargs=-1, required=True, type=click.Path(path_type=Path))
@click.option(
    "--project",
    default=None,
    help="Project key for image-path checks (default: <meta name=docs-project>).",
)
def audit_doc(paths, project):
    """Validate authored plan/doc HTML against the SPA render contract.

    The reckon SPA renders authored HTML faithfully (raw-HTML passthrough): no
    markdown is rendered, the doc's <head><style> is dropped, and images resolve
    against the project mount (/<project>/figures/...). This command flags docs
    that rely on markdown, head-local CSS, or relative image paths — problems
    that render wrong in the SPA.

    Exits non-zero if any ERROR-level problem is found (relative <img src>,
    literal **markdown** in a rendered body, missing required meta).

    Example:

        reckon audit-doc docs/my-plan.html
        reckon audit-doc docs/*.html
    """
    import sys

    from reckon.doccheck import run

    sys.exit(run([str(p) for p in paths], project=project))


@main.command(name="install-skills")
def install_skills():
    """Install reckon skills into ~/.claude/skills/ idempotently.

    Copies each subdirectory of reckon/skills/ into ~/.claude/skills/,
    preserving existing files that are identical and overwriting stale ones.
    Reports: skipped (unchanged) vs updated (changed or new).
    """
    reckon_root = Path(__file__).parent.parent
    skills_src = reckon_root / "skills"
    skills_dst = Path.home() / ".claude" / "skills"

    if not skills_src.is_dir():
        raise click.ClickException(
            f"skills source directory not found: {skills_src}\n"
            "This reckon install has no skills/ directory."
        )

    skills_dst.mkdir(parents=True, exist_ok=True)
    skipped = 0
    updated = 0

    for skill_dir in sorted(skills_src.iterdir()):
        if not skill_dir.is_dir():
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
                click.echo(f"  updated  {skill_dir.name}/{rel}")

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
