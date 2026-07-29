# reckon

Repo-agnostic agile planning system. Three surfaces share one repo and one venv:

| Surface | CLI | What it does |
|---|---|---|
| **reckon server** | `reckon serve` | HTTP backend on `:8765` — serves the SPA, serves shared CSS/JSX, brokers versioned writes to plan HTML semantic elements |
| **reckon MCP** | `reckon mcp` | MCP stdio transport — same writes as the server, callable from Claude Code / Cursor / any MCP client |
| **reckon SPA** | (static) | React SPA under `docs/` — three-column layout (filters · plans · content), Cmd-K palette, plan reading + radial-fan graph, sprint kanban, critical-path graph tab, prompt generation |

The Python distribution is named `reckon-plans`; the import package and
console command remain `reckon`.

## Quick start

```bash
uv sync
uv run reckon serve           # HTTP server on port 8765
uv run reckon serve --port 8766 --mounts /path/to/mounts.json
uv run reckon mcp             # stdio MCP transport
uv run reckon build docs      # portable static site under docs/
```

Once the distribution is published, `uv tool install reckon-plans` installs
the same `reckon` command. A repository checkout can be installed directly
with `uv tool install "git+https://github.com/Simon-McIntosh/reckon"`.

## How it works

Each project keeps its plans under `<repo>/docs/`. Any `.html` file in that
directory is a plan — existence is sufficient. Plan state (status, decisions,
followups, etc.) lives as semantic HTML inside the plan file:

```html
<meta name="plan-status" content="active">
<meta name="plan-impl"   content="0.6">
<!-- inside <main class="plan-doc">: -->
<section data-reckon="decisions" class="r-decisions"> … </section>
<section data-reckon="followups" class="r-followups"> … </section>
```

The server parses each plan's `<meta name="plan-*">` scalars and
`data-reckon` section elements at request time — there are no per-plan
state JSON sidecars. `docs/state/<project>/index.json` holds project-level
config only (sprints, milestones, `active_sprint_id`).

Mounts are configured in `~/docs-server/mounts.json`:

```json
{
  "reckon":      "/home/user/Code/reckon/docs",
  "imas-ambix":  "/home/user/Code/imas-ambix/docs",
  "my-project":  "/home/user/Code/my-project/docs"
}
```

Then open `http://localhost:8765/<project>/` in a browser. The SPA reads state
from the server and renders plans, sprint boards, dependency graph, and prompt
generation.

## Key endpoints

| Method · path | Purpose |
|---|---|
| `GET /<project>/` | SPA shell |
| `GET /<project>/<slug>.html` | Plan prose page |
| `GET /_discover/<project>` | All plans with full parsed state |
| `GET /plan/<project>/<slug>` | Parsed plan state (incl. `version`) |
| `POST /plan/<project>/<slug>` | Dotted-key patch; requires `If-Match: <version>` |
| `GET /state/<project>/index.json` | Project config (sprints, milestones) |

## Frontend

The `docs/` directory is the canonical template. Use `/reckon-sync` (or
`reckon sync <docs-path>`) to copy `docs/_shared/` CSS into a consumer
project’s `docs/` and register it in mounts. JSX components are served live
at `/_ui/<file>` by the reckon server — no per-project copies needed.

For a static deployment, run `reckon build <docs-path>`. The command copies
the UI and shared assets shipped inside the `reckon-plans` wheel, writes a
relative-path SPA index and `.nojekyll`, and bakes discovered plan inventory
into the project index while retaining authored sprint, milestone, timeline,
and blocker state.

To generate a GitHub Pages workflow in a consumer repository:

```bash
uv run reckon sync docs --generate-ci
```

The generated workflow installs uv and invokes the `reckon` command from a
pinned git tag with `uvx --from`. Reckon’s own workflow uses its checked-out
source and lockfile with `uv run --frozen reckon build docs`.

## MCP integration

After `uv sync`, register in `~/.claude/mcp.json`:

```json
{
  "mcpServers": {
    "reckon": { "command": "reckon", "args": ["mcp"] }
  }
}
```

Then any MCP client can call `reckon.read_plan(project, slug)`,
`reckon.patch_plan(...)`, `reckon.lock_decision(...)`, etc. The MCP transport
writes to the same plan HTML elements as `reckon serve` — they are two faces of
one backend.
