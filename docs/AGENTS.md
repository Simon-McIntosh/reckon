# Agent Guidelines — docs

> This file governs the `docs/` sub-tree: how plans, research, and other typed
> documents are authored, laid out, stored, and served. It is loaded
> automatically when work happens inside this directory; repo-wide rules live
> in the root `AGENTS.md`.

### Where docs live and how the server works

Every project keeps its plans under `<repo>/docs/`. The host-wide
**reckon server** (`reckon serve`) at `127.0.0.1:8765` serves them under
stable URL prefixes (`http://localhost:8765/<project>/`).

**The plan HTML is the sole store.** All plan data (status, impl,
decisions, followups, comments, questions, research) lives as semantic
HTML elements in each `.html` file:
- **Scalars** in `<meta name="plan-*">` tags in `<head>`
- **Decisions** as `<div class="r-dec" data-key="…">` elements in `<section data-reckon="decisions">`
- **Followups** as `<article class="r-fu" data-id="…">` elements in `<section data-reckon="followups">`
- **Questions / research / comments** in matching `data-reckon` sections

Live edits (browser clicks, MCP tools) rewrite these HTML elements in place —
there are no per-plan sidecar JSON files.

**Project-level config in index.json.** `docs/state/<project>/index.json`
holds sprints, milestones, `active_sprint_id`, and timeline — not
per-plan state. `reckon-sync` symlinks `<config-home>/state/<project>`
to `<repo>/docs/state/<project>` so the server can write this file
back to the repo.

**Committed run history in crew.json.** `docs/state/<project>/crew.json` sits
beside `index.json` and inherits the same symlink, the same version-paired
write, and the same commit discipline. It holds the durable half of run state:
the team roster and one record per completed run. The transient half — pid,
worktree, log path, phase — lives in `<config-home>/crew/live/<run-id>.json` and
is never committed. `reckon crew complete` moves a record from the second to the
first; `reckon crew recover` reports whatever an interrupted orchestrator left
between them.

Each completed record also carries whatever budget headroom its backend reported,
which is what makes `reckon crew preflight` free: the pre-flight reads the ledger
and the live pointers rather than making a call that would spend the very resource
it is measuring. A backend publishing no headroom records `unknown`, and unknown
never holds a wave — absence of a signal is not evidence of exhaustion.

**Typed HTML roots are semantic.** Plans live under `docs/plans/`, research
under `docs/research/`, and execution evidence under `docs/evidence/`.
`reckon-type` must match the owning root; moving evidence into a plan path or
relabeling it as a plan corrupts the resource graph.

**Typed archival and cumulative evidence.** Frozen plan snapshots live under
`docs/plans/archive/`; frozen research lives under `docs/research/archive/`;
execution outcomes live under `docs/evidence/archive/`. Update one cumulative
`<plan>-landed.html` evidence record as the plan proceeds, using stable anchors
for material sections. A phase transition does not justify another file by
itself: split only a standalone artifact that remains useful when read alone.
Archive resources are excluded from the live inventory.

**HTML is the source of truth.** Do not author markdown plans under
`plans/<slug>.md` and run a generator. Auto-regeneration overwrites
typed archive history and is the anti-pattern that motivated these skills.
Any existing `plans/` markdown is read-only history — archive it under
`plans/archive/` when migrating an old project.

**The server serves the mounted checkout's _working tree_.** `mounts.json`
points at one checkout per project and the server reads whatever is on that
checkout's current branch (MCP writes land there too). Two consequences: an
uncommitted plan edit is branch-local — lost if the checkout switches branches,
and invisible from any other branch; and switching the mounted checkout's branch
silently swaps the served plan state. **General rule: author and maintain a plan
on the branch that implements it, and keep the mounted checkout on that branch.**
In the common single-branch (trunk) case this is automatic — there is no hazard.
It only bites when a plan governs work on a _different_ branch than it lives on
(e.g. a gitflow refactor with the plan on `main` but code landing on `develop`).
Then pin the mounted checkout to the plans branch and do the other-branch work in
a **git worktree** (read/write its state with `checkout_path`, per the
multi-worktree section below) — never switch the mounted checkout out from under
live plan state. Repo-specific branch policy belongs in that repo's own
`AGENTS.md`.

### Server operations

The HTTP server is `reckon serve` (the `reckon` Python package) — **one**
process listening on `127.0.0.1:8765`. It runs as a **systemd user service**
managed by `reckon service`, not by hand and not inside a terminal
multiplexer. Config + state live under reckon's config home. The preferred XDG
location is `~/.config/reckon/`; an existing `~/docs-server/` remains a legacy
fallback only when the XDG directory is absent.

| Intent | Command |
|---|---|
| Deploy / redeploy the unit | `reckon service install` |
| **Restart after changing reckon code** | `reckon service restart` |
| Start / stop | `reckon service start` · `reckon service stop` |
| Unit state, linger, ExecStart | `reckon service status` |
| Server output (`-f` to follow) | `reckon service logs` |
| Remove the unit | `reckon service uninstall` |

- `install` writes `~/.config/systemd/user/reckon.service`, enables lingering,
  and enables + starts the unit. It is idempotent, and restarts the service
  when the rewritten unit differs from the running one.
- **Lingering is mandatory, not cosmetic.** Without
  `loginctl enable-linger`, systemd tears down the per-user manager — and the
  server with it — when the last login session ends. `reckon service install`
  enables it; `reckon service status` reports it.
- **`Restart=on-failure` covers crashes.** A server that dies comes back within
  `RestartSec=5`. Do not treat "the server is gone" as a normal state to fix by
  hand — check `reckon service status` and the log first.
- **Logs go to a file, not the journal**: `<config-home>/logs/server.log`.
  Reading a *user* journal needs membership of `systemd-journal`/`adm`/`wheel`,
  which a plain account on a managed host does not have, so the unit sets
  `StandardOutput=append:`. The file is not rotated — truncate it if it grows.
- Find the process: `ss -ltnp | grep :8765` → the listening `reckon` pid.
  Never `kill` it to restart — systemd will respawn it under you; use
  `reckon service restart`.
- **Server/parse/render code changes are NOT hot** — a running server (and every
  `reckon mcp` stdio server) holds the old code in memory until
  restarted/reconnected. Coordinate restarts with active users of the server and
  MCP connections.
- Mounts: `<config-home>/mounts.json` — re-read on every request, no restart needed
- Plan state (GET): `curl http://127.0.0.1:8765/plan/<project>/<slug>` (parsed semantic elements + `version`)
- Plan patch (POST): `curl -X POST -H 'If-Match: <version>' -d '{"status":"shipped"}' http://127.0.0.1:8765/plan/<project>/<slug>`
- Discovery: `curl http://127.0.0.1:8765/_discover/<project>` (all plans + full parsed state)
- Project config: `curl http://127.0.0.1:8765/state/<project>/index.json`

### reckon MCP tools

The reckon MCP server exposes plan state R/W as structured tools. Compatible
agent clients start it through their MCP config (`reckon mcp` stdio transport).
All agents working with plans MUST use these tools for state mutations — they
rewrite the plan's semantic HTML elements directly.

**Invocation** (if needed manually): `uv run --project ~/Code/reckon reckon mcp`

**The surface is five tools.** Granular reads and both write modes remain
collapsed into `read_plan` and `edit_plan`; graph analysis, conformance
auditing, and run state keep their distinct read-only contracts:

| Tool | Purpose |
|------|---------|
| `read_plan` | Read + context-inject. `read_plan(resource={project,type,id})` defaults to a concise typed summary; `view=detail|history|raw|schema` reveals progressively deeper state. `read_plan(project, slug)` preserves the legacy parsed-state response. `read_plan(project)` (slug omitted) → DISCOVERY; `read_plan()` → mounted projects. |
| `edit_plan` | The one validated write, selected by `mode`. `mode="state"` applies an ordered `ops` list to a working copy, schema-validates it, then writes atomically; verbs are `set` / `append` / `resolve` / `lock` / `move`, and `create=True` scaffolds a plan. `mode="text"` performs one version-safe exact `old_html` → `new_html` replacement for prose, tables, figures, or section bodies and refuses structured state changes. |
| `roadmap` | Read-only DAG scan: all pending work, distinct ready/blocked/deferred sets, lifecycle and stored implementation percentages, sprint order, weighted critical/open paths, and wiring findings. `project="*"` returns a portfolio. |
| `audit` | Schema-conformance audit of every plan in a project + index reindex (WARN/report only — never mutates). Use `view=summary` for counts, `view=detail` for paginated findings, and `view=raw` for the legacy lossless result. Distinct from the CLI `reckon doctor`, which checks infra/skills/mounts, not schema. |
| `crew` | Read-only run state over seven views. `view="ledger"` and `view="summary"` read committed roster and outcome summaries; `view="records"` returns the lossless committed records; `view="live"` reads the never-committed pointers of runs still in flight, each with the classification `reckon crew recover` would give it; `view="drain"` derives the session-closure count and recorded dispositions from those pointers; `view="flight"` reports resolved routing config with the layer that supplied every value; `view="budget"` reports per-backend headroom and whether a wave may open, read from what earlier runs recorded so it spends nothing. Accepts `checkout_path`. Writes are the CLI's (`reckon crew …`) — this tool never mutates. |

**Op vocabulary:** call `read_plan(project, slug, with_schema=True)["op_vocab"]`
for the full `edit_plan` op grammar (it inlines the set/append/resolve/lock/move
+ create rules alongside the schema).

**Mandatory rule:** always call `read_plan` first to get the current `version`
before `edit_plan` — writes are rejected (412) if
`version` doesn't match.
Typed write workflows use `view="raw"` for that version read. Inspection
workflows begin with the default summary and opt into detail only when needed.

**Further detail:** `GET http://127.0.0.1:8765/plan/<project>/<slug>` for
current parsed state, or `/_discover/<project>` for plan inventory with full state.
MCP tool descriptions are self-documenting — query them via the MCP inspector if needed.

#### Multi-worktree agents — `checkout_path` (avoid main-checkout cross-writes)

The MCP server is a **stdio** process; it has **no access to the caller's
working directory**. It resolves every project to the single FIXED docs dir
registered in `mounts.json` — the canonical **main** checkout. So when a
sub-agent runs inside a **git worktree** (a separate checkout of the same
repo, e.g. `.claude/worktrees/agent-XXX`), a bare `edit_plan`/`read_plan`
reads and writes the **main** checkout, not the agent's worktree. Symptoms:
the worktree agent can't commit the change (it isn't in its tree), and the
main checkout is left with a dirty/uncommitted duplicate someone must
reconcile.

`read_plan` and `edit_plan` take an **optional** `checkout_path` — the
absolute path to the desired checkout's **repo root** (the directory that
contains `docs/`). When given, both the plan HTML **and** the
index/project JSON config resolve under `<checkout_path>/docs` (and
`<checkout_path>/docs/state/<project>/` for `index`). Omit it (the default)
to target the registered main checkout — existing single-checkout behaviour
is completely unchanged.

```text
# A worktree agent at /repo/.claude/worktrees/agent-XYZ:
read_plan(project, "index", checkout_path="/repo/.claude/worktrees/agent-XYZ")   # → version from THAT checkout
edit_plan(project, "index", ops=[...], expected_version=<that version>,
          checkout_path="/repo/.claude/worktrees/agent-XYZ")
# → writes <worktree>/docs/state/<project>/index.json; the agent commits it from its own tree.
```

Rules when using `checkout_path`:
- **Pair the read and the write.** `expected_version` must come from a
  `read_plan` that used the **same** `checkout_path`, or the write 412s.
- `edit_plan` now returns **`path`** — the absolute file it wrote — so you
  can reconcile deterministically (`git -C "$(dirname <path>)" status`).
- **Discovery and roadmap are redirected.** `read_plan(project)` with the slug
  omitted and `roadmap(project)` both scan `<checkout_path>/docs` when the
  absolute repository root is supplied. Cross-project dependencies still
  resolve through their registered mounts.

**Recommended workflow for orchestrators with worktree fleets:** prefer that
the **orchestrator** (running in the main checkout) perform `index`/sprint/
followup state mutations, while worktree workers author their plan **HTML**
in their own tree and `read_plan(..., checkout_path=…)` for state. When a
worktree worker must mutate state itself, it passes `checkout_path=<its repo
root>` and commits the resulting `path` from its own tree.

#### Repository ownership and relationship wiring

Plan placement is an architecture decision, not a filename decision. Before
creating or relocating a plan, read the target and nearest-path `AGENTS.md`,
the project resource's optional `scope` policy, and neighboring mounted
projects with overlapping responsibilities. State what the destination owns,
what it consumes, and what routes elsewhere. Never keep one live plan in two
repositories.

Projects may publish allocation guidance in their project resource:

```json
{
  "scope": {
    "owns": ["executable responsibilities of this repository"],
    "excludes": ["similar work owned elsewhere"],
    "routes": [{"work": "description", "project": "destination"}]
  }
}
```

Relationship semantics are binding:

- `depends_on` is a hard executable prerequisite: the plan cannot close until
  the target plan completes.
- `informs` is a research, specification, evidence, or reference input. It does
  not block execution.
- `blocks` names downstream work unlocked by this plan.
- sprint item `blocked_by` names explicit human/external blockers; do not
  duplicate derived prerequisites there.

Run `roadmap` after any relationship, sprint, status, creation, or relocation
write. Error-level wiring findings must be repaired before execution. Every
actionable new plan belongs to exactly one sprint with matching `plan-sprint`,
or carries an explicit backlog decision.

### Forwarding the port (from a laptop)

Do **not** add `LocalForward` to `~/.ssh/config` under `Host iter` —
it pollutes every SSH connection and conflicts with the
reverse-tunnel ports the `iter-reverse-tunnel.service` manages. Use
the `imas-codex tunnel` CLI instead:

```bash
# ad-hoc:
uv run --project ~/Code/imas-codex imas-codex tunnel start iter --docs
# persistent (autossh + systemd-user):
uv run --project ~/Code/imas-codex imas-codex tunnel service install iter --docs
# bundled with neo4j/embed/llm/vllm (one autossh process):
uv run --project ~/Code/imas-codex imas-codex tunnel service install iter
```

Port (8765) and location are configurable in
`~/Code/imas-codex/pyproject.toml` under
`[tool.imas-codex.docs-server]`.

### Decision capture model

Decisions belong to the page that **owns** them, not to a central
decisions doc. Each plan's `<section data-reckon="decisions">` carries
`<div class="r-dec" data-key="…">` elements — one per decision. The SPA
renders these interactively and POSTs updates to `/plan/<project>/<slug>`
as dotted patches (e.g. `decisions.scan-strategy.choice`). The server sets
`data-choice` on the matching `.r-dec` and marks the chosen `<button class="r-opt chosen">`.
A separate `decisions.html` is a **read-only aggregator** that reads each
plan's parsed state and shows current choices — it never writes.

### Mandatory one-line invocation (§05) on every followup

Every followup written into the plan's followups section MUST set its `prompt`
(in `<pre class="r-fu-prompt">`) to exactly one copy-paste line:

```
/reckon-ship <slug> [§N]
```

The live plan owns context, decisions, evidence inputs, constraints, and
done-when criteria. Copying them into a handoff creates a second stale source
of truth. Runtime model, effort, concurrency, worktree, and file-scope choices
belong to the new session and its coordinator, not to persisted followups.

The reckon-edit and reckon-ship skills enforce this. A followup without a
non-empty one-line invocation is a hard failure.

### Dissent flow (§07) — disagreeing with a locked decision

A locked decision is a contract. If an agent (or human) disagrees, it
MUST NOT silently re-lock with a different choice — that erases the
audit trail. Instead:

1. Write a followup with
   `recommends_skill: "/reckon-edit <slug> --reopen <decision-key>"`.
2. In the body, lay out the locked choice, what evidence has changed,
   and what the agent proposes instead.
3. The followup becomes the NEXT card. A human (or coordinator agent)
   reviews and either:
   - Accepts: runs `/reckon-edit --reopen`, which captures the old
     decision into a per-stage record (`<slug>-<key>-locked.html`)
     and reopens the row on the evergreen page.
   - Rejects: writes a counter-followup explaining why the locked
     choice stands.

Either way, the chain is preserved.

### Plan semantic data model

All plan state lives as HTML elements in the `.html` file. There are no
per-plan `state/<project>/<slug>.json` sidecar files.

**Scalars** (`<meta name="plan-*">` in `<head>`):

| Meta name | Values | Notes |
|---|---|---|
| `plan-slug` | kebab-case | Optional; defaults to filename stem |
| `plan-status` | `draft`/`pending`/`active`/`in-progress`/`blocked`/`shipped`/`done`/`abandoned` | Server-written |
| `plan-impl` | `0.0`–`1.0` | Progress fraction; `reckon-ship` sets it (shipped/total) per landing — not auto-computed |
| `plan-version` | integer | Server-owned concurrency counter; never author |
| `plan-roi` | `high`/`mid`/`low` | |
| `plan-effort` | `S`/`M`/`L`/`XL` | |
| `plan-milestone` | e.g. `M2` | |
| `plan-sprint` | e.g. `S4` | |
| `plan-tier` | `haiku`/`sonnet`/`opus` | |
| `plan-summary` | one-line text | |
| `plan-depends-on` | comma-separated slugs | |
| `plan-modified` | `YYYY-MM-DD` | Server-written on each POST |

**Decisions** (`<section data-reckon="decisions">` → `<div class="r-dec" data-key="…">`):
- `data-choice=""` → open; non-empty → locked (option value OR free text)
- `data-by`, `data-when` → set when locked
- `<p class="r-dec-q">` — the question
- `<p class="r-dec-ctx">` — optional context
- `<p class="r-dec-opts">` — `<button class="r-opt [chosen]" data-value="…">label</button>` options
- `<p class="r-dec-rat">` — rationale text
- No `<button>` elements → pure free-form decision

**Followups** (`<section data-reckon="followups">` → `<article class="r-fu" data-id="…">`):
- `data-status` — `open` / `resolved`
- `data-tier`, `data-written-by`, `data-written-at`, `data-recommends-skill`
- `data-resolved-at`, `data-resolved-by` — set when resolved
- `<h4 class="r-fu-title">`, `<div class="r-fu-body">`
- `<pre class="r-fu-prompt">` — §05 one-line invocation (MANDATORY)
- `<p class="r-fu-outcome">` — added on resolve

**Questions** (`<section data-reckon="questions">` → `<div class="r-q" data-id="…">`):
- `data-status` — `open` / `resolved`; `data-section`, `data-opened-by`, `data-opened-at`
- `data-resolved-at`, `data-resolved-by`; `<p class="r-q-body">`; `<p class="r-q-resolution">`

**Research** (`<section data-reckon="research">` → `<div class="r-research" data-id="…">`):
- `data-type`, `data-source`, `data-added-by`, `data-when`, `data-url`
- `<span class="r-research-title">` (with optional `<a>` link)

**Comments** (`<section data-reckon="comments">` → `<div class="r-comment" data-id="…">`):
- `data-section` — anchors to a section `id`; `data-who`, `data-when`, `data-quote`
- `<div class="r-comment-body">`

`plan-version` is server-owned — never write it. Sections may be absent if empty.
The authoritative reference is `~/Code/reckon/PLAN-FORMAT.md`.

### CSS architecture

Pages load two shared stylesheets:

1. `/_shared/foundation.css` — design tokens, body, shell, primitives
2. `/_shared/dashboard.css` — plan-domain widgets (status, kanban, milestone stepper, NEXT card)

Canonical source lives in `~/Code/reckon/docs/_shared/`.
The reckon server exposes them at `/_shared/<file>` (live, from reckon install)
and `/_ui/<file>` for JSX components. Plan pages served by the live server link
to these absolute routes. For GitHub Pages static deployment, run `reckon sync`
to copy CSS to `docs/_shared/`, or `reckon build` for a fully self-contained
static bundle including JSX.

### Plan vocabulary (canonical, cross-repo)

Use these terms exactly. The skills, templates, and `index.json` schema
all assume this vocabulary.

| Term | Definition |
|---|---|
| **Plan** | Unit of work captured as an HTML doc with lifecycle phase, status, metadata. Lives at `docs/<slug>.html`. |
| **Sprint** | Ordered, scoped collection of plan items for execution within a time window. IDs are `S1`, `S2`, … Has a theme, optional start/end dates, status. |
| **Milestone** | Named target tied to plans/sprints that marks meaningful project progress. IDs are `M0`, `M1`, … Has status and evidence (commits/PRs). |
| **Blocker** | Condition preventing an item from progressing. Has owner, next-step, and a count of plans it gates. |
| **Question** | Unresolved design question with enumerated options + origin plan. |
| **Decision** | A resolved choice between options. Captured per-page via `state.js`. |
| **Phase** | Lifecycle stage of the plan as a whole: `draft → active → in-progress → blocked → shipped → archived`. Encoded as the page's status badge or its filename suffix (`<plan>-shipped.html`). |
| **Status** (per item) | One of: `pending`, `active` / `in-progress`, `blocked`, `shipped` / `done`, `superseded`, `abandoned`, `historical`. |
| **Implementation fraction** | `[0.0, 1.0]` — `count(items where status=shipped) / count(items)`. Computed; rendered as the plan's progress indicator. |
| **ROI** | `high` / `mid` / `low`. Drives sprint ordering. |
| **Effort** | `S` / `M` / `L` / `XL` t-shirt sizing. |

### Typed archives and cumulative evidence

Archive paths retain their type. A plan normally produces one cumulative
execution record, updated as sections land.

| File | Location | Use when |
|---|---|---|
| `<plan>.html` | `docs/plans/` | Evergreen — most-recent design view (always present) |
| `<plan>-landed.html` | `docs/evidence/archive/` | Cumulative commits, tests, artifacts, metrics, and negative findings, with stable section anchors |
| `<plan>-<decision>-locked.html` | `docs/plans/archive/` | Frozen irreversible decision snapshot |
| `<plan>-final.html` | `docs/plans/archive/` | Frozen plan snapshot at closure |
| `<topic>.html` | `docs/research/archive/` | Frozen research/reference input |

Do not create a landed file per section, commit, or test wave. Split a second
evidence resource only when it is a materially independent artifact that is
useful when read alone.

### Repo layout — one canonical format

Every repo uses the same layout:

- `docs/plans/<slug>.html` — evergreen plan pages
- `docs/research/<slug>.html` — live research/reference pages
- `docs/evidence/archive/<slug>-landed.html` — cumulative execution evidence
- `docs/<type>/archive/` — frozen resources that retain their declared type
- `docs/state/<project>/index.json` — project config only (sprints, milestones, `active_sprint_id`)
- `docs/state/<project>/crew.json` — the committed run ledger: team roster plus one record per completed run (server-written, version-paired, git-committed like `index.json`)
- `docs/_shared/` — CSS files copied from canonical reckon source
- `docs/index.html` — SPA entry point (managed by `reckon-sync`)

Per-plan state lives in each HTML file's semantic elements — not in any JSON sidecar.

### `index.json` schema (project config only)

```json
{
  "updated": "YYYY-MM-DDTHH:MM:SS",
  "project": "<project>",
  "data": {
    "active_sprint_id": "S2",
    "sprints": [{
      "id": "S1", "theme": "...", "description": "...",
      "starts": "YYYY-MM-DD", "ends": "YYYY-MM-DD",
      "status": "planned|active|done",
      "items": [{
        "slug": "<plan-slug>", "title": "...",
        "roi": "high", "effort": "M", "milestone": "M2",
        "why_now": "...", "done_when": "...",
        "status": "pending", "tier": "sonnet",
        "blocked_by": ["blocker-id"]
      }]
    }],
    "milestones": [{
      "id": "M2", "name": "...", "status": "...",
      "evidence": ["commit SHAs"], "depends_on": ["M1"]
    }],
    "timeline": []
  }
}
```

Sprint items reference plan slugs; their current state (impl, decisions, followups)
is always read from the plan's HTML via `GET /plan/<project>/<slug>` or
`/_discover/<project>`.

### What goes wrong without these skills

The recorded incidents that motivated this skill set:

1. **Skill not loaded.** "Implement the X.html plan" → edited markdown
   and re-ran a generator instead of editing HTML directly. The
   reckon-* skills' descriptions are tuned so this no longer slips by.
2. **Evidence fragmented.** Each landed section produced a tiny standalone
   page, obscuring the plan's coherent result and adding decorative document
   chrome. `reckon-edit` and `reckon-ship` now require one cumulative evidence
   record by default and a new file only for a standalone artifact.
3. **Plan HTML not updated.** Decisions discussed in chat never reached
   the plan's semantic elements. `reckon-edit` requires the MCP call
   or `POST /plan/<project>/<slug>`.
4. **`git commit -a` swept peer fleet edits.** Multiple workers,
   non-overlapping scopes, but `-a` swept WIP into the wrong commit.
   `reckon-ship`'s dispatch boilerplate explicitly bans `-a`/`-am`
   and `git add -A/./*` in every worker prompt.
5. **Decisions baked into a generator.** Centralised decision list in
   a Python script meant editing the script to add a decision. The
   current model: decisions live as `.r-dec` elements in the plan's
   `<section data-reckon="decisions">`, written by `reckon-edit` or the browser SPA.
6. **Worker routing did not match the task.** The coordinator assigned a
   worker without enough reasoning or verification capacity for coupled solver
   edits. Select and state the runtime model and effort from the actual task
   requirements; do not rely on a fixed relative hierarchy.
