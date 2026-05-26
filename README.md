# reckon

Repo-agnostic agile planning system. Three surfaces share one repo and one venv:

| Surface | CLI | What it does |
|---|---|---|
| **reckon server** | `reckon serve` | HTTP backend on `:8765` — serves the SPA, serves shared CSS/JSX, brokers versioned writes to plan state JSON files |
| **reckon MCP** | `reckon mcp` | MCP stdio transport — same writes as the server, callable from Claude Code / Cursor / any MCP client |
| **reckon SPA** | (static) | React SPA under `docs/` — three-column layout (filters · plans · content), Cmd-K palette, plan reading + radial-fan graph, sprint kanban, critical-path graph tab, prompt generation |

## Quick start

```bash
uv run reckon serve           # HTTP server on port 8765
uv run reckon serve --port 8766 --mounts /path/to/mounts.json
uv run reckon mcp             # stdio MCP transport
```

## How it works

Each project keeps its plans under `<repo>/docs/` and its state under `<repo>/docs/state/<project>/`. Mounts are configured in `~/docs-server/mounts.json` (the directory path is historical — the server itself is the reckon server):

```json
{
  "reckon":      "/home/user/Code/reckon/docs",
  "imas-ambix":  "/home/user/Code/imas-ambix/docs",
  "my-project":  "/home/user/Code/my-project/docs"
}
```

Then open `http://localhost:8765/<project>/` in a browser, or load the named entry directly at `http://localhost:8765/<project>/reckon.html`. The SPA reads state from the server and renders plans, sprint boards, dependency graph, and prompt generation.

## Frontend

The `docs/` directory is the canonical template. Use `/reckon-sync` (or `reckon sync <docs-path>`) to copy `docs/_shared/` and `docs/ui/` into a consumer project's `docs/` and register it in mounts.

## MCP integration

After `uv pip install -e .`, register in `~/.claude/mcp.json`:

```json
{
  "mcpServers": {
    "reckon": { "command": "reckon", "args": ["mcp"] }
  }
}
```

Then any MCP client can call `reckon.read_plan(project, slug)`, `reckon.patch_plan(...)`, `reckon.lock_decision(...)`, etc. The MCP transport writes to the same state JSON files as `reckon serve` — they are two faces of one backend.
