# reckon

Repo-agnostic agile planning system. Provides a Python HTTP server and a React SPA for browsing, interacting with, and generating agent prompts from structured plan state.

## Quick start

```bash
uv run reckon serve           # start server on port 8765
uv run reckon serve --port 8766 --mounts /path/to/mounts.json
```

## How it works

Each project keeps its plans under `<repo>/docs/` and its state under `<repo>/docs/state/<project>/`. Add the project to `~/docs-server/mounts.json`:

```json
{
  "imas-ambix": "/home/user/Code/imas-ambix/docs",
  "my-project":  "/home/user/Code/my-project/docs"
}
```

Then open `http://localhost:8765/<project>/` in a browser. The SPA reads state from the server and renders plans, sprint boards, and decision capture UI.

## Frontend

The `docs/` directory is the canonical template. Copy `docs/_shared/` and `docs/ui/` into your project's `docs/` when setting up a new project with `/plan-init`.
