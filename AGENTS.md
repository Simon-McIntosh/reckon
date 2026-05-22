# Agent Guidelines — reckon

> Shared guardrails live in `~/.agents/AGENTS.md`. This file covers repo-specific rules only.

## Project

**reckon** is a repo-agnostic agile planning system. Primary branch: `main`.

The repo provides:
- `reckon/serve.py` — Python HTTP server for serving plan docs and state (port 8765 by default)
- `docs/` — Canonical React/JSX SPA for browsing, navigating, and acting on plans

## Python

- Package manager: uv (`uv run reckon serve` to start the server)
- Python ≥ 3.12, dynamic versioning via hatch-vcs
- No tests yet — add under `tests/` as needed, run with `uv run pytest`

## Frontend

The docs/ directory is the canonical planning SPA template:
- Pure client-side React 18 + JSX compiled in-browser via Babel standalone (no build step)
- CSS: docs/_shared/foundation.css, docs/_shared/dashboard.css
- JSX components: docs/ui/ (v7-shell.jsx is the root)
- State is loaded at runtime from `state/<project>/index.json` via the docs-server

## Repo-agnostic principle

Never hardcode a project name (imas-ambix, imas-efit, etc.) in reckon itself.
Project identity comes from `meta[name="docs-project"]` in the served HTML and from mounts.json.
