# reckon

Repo-agnostic agile planning system. Four surfaces share one repo and one venv:

| Surface | CLI | What it does |
|---|---|---|
| **reckon server** | `reckon serve` (`reckon service` to run it as a daemon) | HTTP backend on `:8765` — serves the SPA, serves shared CSS/JSX, brokers versioned writes to plan HTML semantic elements |
| **reckon MCP** | `reckon mcp` | MCP stdio transport — same writes as the server, callable from Claude Code / Cursor / any MCP client |
| **reckon crew** | `reckon crew dispatch` (`observe`, `resume`, `attach`, `list`, `stop`, `complete`, `recover`, `member`, `ledger`) | Backend-agnostic worker dispatch — validates the node contract, resolves flight config, creates its detached worktree, returns JSON describing the spawned run or in-harness directive, and promotes each finished run into the owning repository's committed ledger |
| **reckon SPA** | (static) | React SPA under `docs/` — three-column layout (filters · plans · content), Cmd-K palette, plan reading + radial-fan graph, sprint kanban, critical-path graph tab, prompt generation |

The Python distribution is named `reckon-plans`; the import package and
console command remain `reckon`.

## Quick start

```bash
uv sync
uv run reckon serve           # HTTP server on port 8765
uv run reckon serve --port 8766 --mounts /path/to/mounts.json
uv run reckon mcp             # stdio MCP transport
uv run reckon crew dispatch --project sample --plan plan-alpha --section s3 \
  --role implement --node docs-readme --goal "Document the crew CLI" \
  --done-when "grep -c 'reckon crew' README.md returns 3 or more" \
  --write-path README.md --time-budget 20m \
  --manifest /tmp/docs-readme-manifest.md --session example
uv run reckon crew observe --run <run-id> --project sample
uv run reckon crew attach --run <run-id> --task <harness-task-id>  # in-harness runs only
uv run reckon crew complete --run <run-id> --gate passed --commit <sha>
uv run reckon crew recover    # what an interrupted orchestrator left behind
uv run reckon build docs      # portable static site under docs/
uv run reckon migrate-layout docs --check  # collision-safe migration preview
```

`reckon crew dispatch` is the single launch instruction for every configured
backend. It refuses malformed nodes before creating a worktree, then returns
one JSON document whose `launch` field tells the caller whether a process was
spawned or an in-harness task must be launched and bound with `attach`. Use the
returned run id with `observe`; if a worker reports `NEEDS-HELP:`, answer it in
the same session with `resume` rather than dispatching a replacement.

A run has two homes over its life. In flight it is a pointer under reckon's
config home — pid, worktree, log, phase — which churns every few seconds and is
never committed. `complete` promotes the finished record into the owning
repository's ledger at `docs/state/<project>/crew.json`, committed beside
`index.json` and version-paired the same way, then deletes the pointer — in that
order, so an interruption leaves a recoverable pointer rather than a lost
record. `recover` classifies whatever is left: running, completed-but-unpromoted
(with its manifest path), or abandoned. It repairs the record only; it never
force-removes a worktree.

## Running the server as a service

`reckon serve` in a terminal dies with the terminal and never comes back on
its own. For a persistent deployment, install it as a systemd **user** service:

```bash
uv run reckon service install   # write the unit, enable lingering, start it
uv run reckon service restart   # the command to run after changing reckon code
uv run reckon service status    # unit state, lingering, effective ExecStart
uv run reckon service logs -f   # follow the server's output
```

`install` is idempotent, and restarts the service when the rewritten unit
differs from the running one. The unit sets `Restart=on-failure`, so a crashed
server returns within five seconds, and enabling lingering keeps it alive after
you log out — without it, systemd stops the per-user manager and every unit it
owns at the end of your last login session.

Output is appended to `<config-home>/logs/server.log` rather than the journal,
because reading a *user* journal requires a privileged group membership that a
plain account on a managed host does not have. The file is not rotated.

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

For a host-wide reviewed migration, snapshot the complete active mount registry
and select only the repositories whose write scope is authorised:

```bash
uv run reckon migrate-fleet \
  --run-id 20260729T180000Z \
  --apply-project reckon
```

The command discovers the effective `mounts.json` at runtime, records its hash,
creates a content-bearing before snapshot for every registered repository, and
writes an incremental machine ledger below the Reckon config home. Unselected,
dirty, detached, conflicting, or otherwise unsafe repositories receive an
explicit terminal `deferred` row; they are never silently omitted. A selected
repository is migrated in a temporary copy, where capability conversion, typed
layout, distributed state, document/schema/relationship audits, concise MCP
reads, and a static build must all pass before exact files are installed.

Repository commits remain a coordinator responsibility. After committing and
pushing a verified row, attach the durable evidence with:

```bash
uv run reckon migration-record <ledger.json> <project> <commit> origin/main
```

Rollback is exact-path only and requires both the content-bearing snapshot and
the migration ledger's changed-path list:

```bash
uv run reckon migration-rollback <snapshot.zip> <docs-path> \
  --path plans/example.html --path example.html
```

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
`reckon.edit_plan(...)`, `reckon.roadmap(project)`, `reckon.audit(project)`, and
`reckon.crew(project, view=...)`. The MCP transport writes to the same semantic
HTML elements as `reckon serve` — they are two faces of one backend.

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
