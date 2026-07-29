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
uv run reckon migrate-layout docs --check  # collision-safe migration preview
```

Once the distribution is published, `uv tool install reckon-plans` installs
the same `reckon` command. A repository checkout can be installed directly
with `uv tool install "git+https://github.com/Simon-McIntosh/reckon"`.

## How it works

Each project keeps typed resources under `<repo>/docs/plans/`,
`docs/research/`, `docs/evidence/`, and `docs/sprints/`. Stable identity is
project + type + slug, independent of the relative file path. Mixed flat/typed
repositories remain readable; `reckon migrate-layout docs` moves files only
when invoked explicitly. Plan state (status, decisions, followups, etc.) lives as semantic
HTML inside the plan file:

```html
<meta name="plan-status" content="active">
<meta name="plan-impl"   content="0.6">
<!-- inside <main class="plan-doc">: -->
<section data-reckon="decisions" class="r-decisions"> … </section>
<section data-reckon="followups" class="r-followups"> … </section>
```

The server parses each plan's `<meta name="plan-*">` scalars and
`data-reckon` section elements at request time — there are no per-plan
state JSON sidecars. Project workflow state is independently versioned under
`docs/sprints/`, `docs/milestones/`, `docs/blockers/`, and
`docs/state/<project>/timeline.html`; `project.json` is identity/presentation
only. A retained `index.json` is a frozen compatibility snapshot.

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
| `GET /<project>/<type-root>/<slug>` | Canonical typed prose page |
| `GET /<project>/<slug>.html` | Flat compatibility redirect |
| `GET /_discover/<project>` | All plans with full parsed state |
| `GET /plan/<project>/<type-root>/<slug>` | Typed parsed state (incl. `version`) |
| `GET /plan/<project>/<slug>` | Compatibility plan-state read |
| `POST /plan/<project>/<slug>` | Dotted-key patch; requires `If-Match: <version>` |
| `GET /state/<project>/index.json` | Composed compatibility view (read-only after migration) |

## Frontend

The `docs/` directory is the canonical template. Use `/reckon-sync` (or
`reckon sync <docs-path>`) to copy `docs/_shared/` CSS into a consumer
project’s `docs/` and register it in mounts. JSX components are served live
at `/_ui/<file>` by the reckon server — no per-project copies needed.

For a static deployment, run `reckon build <docs-path>`. The command copies
the UI and shared assets shipped inside the `reckon-plans` wheel, writes a
relative-path SPA index and `.nojekyll`, and writes a derived
`projection.json`. It never rewrites the frozen migration-source index.

Split a legacy project index explicitly with:

```bash
uv run reckon migrate-project-state docs --project <project>
```

The migration snapshots the source, proves composed parity, installs typed
resources, then publishes the distributed-format marker last.

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
`reckon.edit_plan(...)`, and `reckon.audit(project)`. The MCP transport writes
to the same semantic HTML elements as `reckon serve` — they are two faces of
one backend.

Typed reads are progressive. A resource selector defaults to a concise human
summary; explicit views reveal current detail, paginated history, lossless
storage state, or schema:

```python
read_plan(resource={"project": "sample", "type": "plan", "id": "plan-alpha"})
read_plan(
    resource={"project": "sample", "type": "sprint", "id": "S1"},
    view="detail",
)
read_plan(
    resource={"project": "sample", "type": "plan", "id": "plan-alpha"},
    view="raw",
)
audit(project="sample", view="summary")
```

`summary` never includes full followup prompts. Use `view="detail"` with
`include_prompts=True` when a prompt is specifically needed. `history` and
large discovery/audit detail responses use `cursor` plus a bounded `limit`.
Calls that omit both `resource` and `view` preserve the legacy response shape.
Before distributed project-state activation, typed sprint/project reads are
safe projections of the canonical legacy index and identify that source in
their warning/state metadata. Writes continue through `slug="index"` until the
explicit migration activates independently versioned named resources.
