# reckon

Repo-agnostic agile planning system. Three surfaces share one repo and one venv:

| Surface | CLI | What it does |
|---|---|---|
| **reckon server** | `reckon serve` | HTTP backend on `:8765` — serves the SPA, serves shared CSS/JSX, brokers versioned writes to plan HTML islands |
| **reckon MCP** | `reckon mcp` | MCP stdio transport — same writes as the server, callable from Claude Code / Cursor / any MCP client |
| **reckon SPA** | (static) | React SPA under `docs/` — three-column layout (filters · plans · content), Cmd-K palette, plan reading + radial-fan graph, sprint kanban, critical-path graph tab, prompt generation |

## Quick start

```bash
uv run reckon serve           # HTTP server on port 8765
uv run reckon serve --port 8766 --mounts /path/to/mounts.json
uv run reckon mcp             # stdio MCP transport
```

## How it works

Each project keeps its plans under `<repo>/docs/`. Any `.html` file in that
directory is a plan — existence is sufficient. Plan state (status, decisions,
followups, etc.) lives in an embedded island in each HTML file:

```html
<script type="application/json" id="reckon-state">
{ "slug": "my-plan", "status": "active", "decisions": {}, "followups": [] }
</script>
```

The server parses each plan's island at request time — there are no per-plan
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
| `GET /_discover/<project>` | All plans with full island state |
| `GET /plan/<project>/<slug>` | Raw island (incl. `version`) |
| `POST /plan/<project>/<slug>` | Dotted-key patch; requires `If-Match: <version>` |
| `GET /state/<project>/index.json` | Project config (sprints, milestones) |

## Frontend

The `docs/` directory is the canonical template. Use `/reckon-sync` (or
`reckon sync <docs-path>`) to copy `docs/_shared/` CSS into a consumer
project's `docs/` and register it in mounts. JSX components are served live
at `/_ui/<file>` by the reckon server — no per-project copies needed.

## MCP integration

After `uv pip install -e .`, register in `~/.claude/mcp.json`:

```json
{
  "mcpServers": {
    "reckon": { "command": "reckon", "args": ["mcp"] }
  }
}
```

Then any MCP client can call `reckon.read_plan(project, slug)`,
`reckon.patch_plan(...)`, `reckon.lock_decision(...)`, etc. The MCP transport
writes to the same plan HTML islands as `reckon serve` — they are two faces of
one backend.
