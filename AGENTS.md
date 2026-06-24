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
- JSX components: docs/ui/ (shell.jsx is the root)
- Plan state is loaded at runtime from the plan's semantic HTML elements (parsed by the server via `GET /plan/<project>/<slug>`); project config from `state/<project>/index.json`

## Repo-agnostic principle

Never hardcode a project name (imas-ambix, imas-efit, etc.) in reckon itself.
Project identity comes from `meta[name="docs-project"]` in the served HTML and from mounts.json.

## HTML-first plans

Plain markdown remains fine ONLY for short prose (READMEs, brief notes).
Anything project-level that needs tables, diagrams, status tiles,
side-by-side comparisons, interactive decision capture, or shared
state across humans and agents is a **plan**, and plans are HTML.

**Non-plan structured docs are ALSO HTML.** RCAs, incident reports,
SDCC/ops tickets, design reviews, explainers and dashboards are NOT
markdown — author them with `reckon-create` using `reckon-type=doc`
(a standalone HTML page in `docs/`, no plan lifecycle). If you catch
yourself writing a `docs/*.md` for anything with a table, a timeline,
or a status, stop and use `reckon-create` instead. (This rule exists
because an RCA + an SDCC ticket were authored as markdown on 2026-05-27
when the routing table still pointed at a since-removed `html-docs`
skill — they had to be re-authored as HTML.)

**Reckon MCP server down is NOT an excuse for markdown.** `reckon-create`
writes the HTML file directly from its template; the server is only
needed to mutate plan *state* (status/impl/decisions/followups) later.
When the MCP is down: still author the HTML doc/plan, and apply state
mutations via MCP once it reconnects (or note the deferral).

### Skills you must use — never freelance

Plans have a specific skill set. Do not edit `docs/*.html`
without invoking the matching skill first; the skills bake in rules
that ad-hoc edits routinely violate (per-stage archival, state
writeback, content parity, fleet safety).

| Intent | Skill | Slash command |
|---|---|---|
| Create a brand-new plan | `reckon-create` | `/reckon-create <slug>` |
| Edit an existing plan, lock a decision, record an outcome, write a followup | `reckon-edit` | `/reckon-edit <slug>` |
| Implement the work a plan describes; record outcomes; followup with §05 prompt | `reckon-ship` | `/reckon-ship <slug> [section]` |
| Sprint / milestone / roadmap state (the project index) | `reckon-sprint` | `/reckon-sprint` |
| Pure-read inspection across all plans in this repo | `reckon-status` | `/reckon-status` |
| Set up or refresh reckon infra in a repo (CSS, mounts, state dir, symlink) | `reckon-sync` | `/reckon-sync` |
| Non-plan docs (RCAs, incident reports, tickets, reviews, explainers, dashboards) | `reckon-create` (with `reckon-type=doc`) | `/reckon-create <slug>` |

**Trigger discipline.** When the user says "the X plan", read the
relevant skill's SKILL.md **before** touching any file. The skills
live at `~/.claude/skills/reckon-*/SKILL.md`. They are short; reading
them is cheap.

### Plan-state integrity (mandatory — fix for the "silent bypass" failure mode)

**Failure mode that motivated this section.** 2026-05-21: shipped
plans-infra in dotfiles + ambix without updating the plan's state
to reflect that it shipped. The plan-system told a different story
than the codebase. RCA: the coordinator authored a custom sub-agent
dispatch prompt instead of routing through `/reckon-ship` (which has
the followup-write requirement baked in), AND failed to write a
closing followup on the parent plan when work completed.

**The mandate.** Any change to a plan's state — implementation lands,
decision resolves, status changes, blocker clears, sprint moves —
MUST be reflected in the **plan's semantic HTML** (via MCP tools or
`POST /plan/<project>/<slug>`) **in the same turn that the change
happens**.

**The HTML is the sole store.** There are no per-plan
`state/<project>/<slug>.json` sidecars. All plan data (status, impl,
decisions, followups) lives as `<meta name="plan-*">` scalars and
`data-reckon` section elements in the plan HTML. The server rewrites
those elements on every successful POST.

Concretely:

1. **When work lands on a plan**:
   - `status` updated (`active` → `shipped`/`done` when fully done;
     `active` → `blocked` when stalled; etc.)
   - `impl` advanced toward 1.0
   - `modified` set to today (server-written on each POST)
   - **The driving followup MUST be resolved** with `resolved_at`,
     `resolved_by`, `outcome` describing what landed
   - **A new followup MUST be written** with the §05 prompt template
     for whatever comes next — or with `outcome: "done — no followup"`
     when the chain truly closes
   - `version` will be incremented by the server on each POST (do
     not set it client-side)

2. **When a decision is resolved**:
   - `decisions.<key>.choice`, `rationale`, `when`, `by` updated
     via the MCP `edit_plan` `lock` op or `POST /plan/<project>/<slug>`
     with a dotted patch. The server sets `data-choice` on the `.r-dec`
     element in the HTML. Direct HTML edits are permitted ONLY if you
     announce the bypass reason in your reply.

3. **Coordinator dispatch contract.** When dispatching a Sonnet (or
   any) worker to implement plan work, the dispatch prompt MUST:
   - Include the **§05 prompt template** with locked decisions, open
     decisions to surface, and a Done-when list
   - **Require the worker to append a followup** to the plan's followups
     section (via MCP or POST) before declaring done
   - **Require the worker to resolve its driving followup** (if it
     was dispatched from one) with `resolved_at`, `outcome`

   When all workers land, the **coordinator MUST verify the followup
   landed** before marking the plan-task as shipped. If a worker
   omitted the followup, the coordinator writes it themselves
   (reflecting what the worker reported).

4. **Eat-the-dog-food check.** Before marking any reckon-ship work
   "done" in chat, verify the plan-system itself reflects the work:
   - `GET /plan/<project>/<slug>` → `status` matches reality
   - The driving followup is resolved (`data-status="resolved"` on its `<article class="r-fu">`)
   - A next followup or `outcome: "done — no followup"` is present
   - `version` has incremented
   If any of those is false, you bypassed the skill. Fix it before
   moving on.

5. **State-bypass announcement.** Any agent that edits plan HTML
   elements directly (rather than via MCP tools or `/reckon-edit`)
   MUST announce "bypassing /reckon-edit because X" in their reply.
   Silent bypasses are exactly the failure mode being prevented here.

This is enforced by discipline, not by tooling — but the discipline
is binding. Surface a violation explicitly when you spot one.

## Plan Lifecycle Invariants

The `plan-lifecycle-hygiene` review proposed six lifecycle-hygiene mechanisms.
Treat them as the operating checklist below until the remaining design-review
questions are resolved.

1. **Write-time lifecycle invariants** — **pending lead design review**.
   Do not add hard/soft write-time enforcement for `impl`, landed summaries, or
   research-doc closure until the lead locks the strictness choice.
2. **Audit cadence** — **implemented** via `reckon audit`.
   Run `uv run reckon audit` (or `uv run reckon audit --project <name>`) to scan
   mounted projects for:
   - `STALE`: active plans older than 30 days with `plan-impl < 1.0`
   - `MISSING_IMPL`: shipped/done plans with missing or zero `plan-impl`
   - `STALE_RCA`: `reckon-type=research` docs older than 60 days whose status is
     not `done`/`archived`
   The command exits with code 1 when any `MISSING_IMPL` row is present, so it is
   safe to wire into CI or a weekly hygiene job.
3. **Archive waves** — **policy active; automation pending**.
   Run a quarterly archive pass that sets `plan-archived=1` on done or
   superseded docs older than 90 days. Per-stage records continue to live under
   `docs/archive/`; the default dashboard should stay focused on live docs.
4. **Plan-creation diet** — **pending lead design review**.
   Do not tighten `reckon-create` pre-flight behaviour until the lead decides how
   hard to bias new work toward existing plans.
5. **Sprint-close gate** — **pending lead design review**.
   Do not add close-sprint refusal logic for unresolved followups / unset impl /
   missing landed summaries until the lead chooses the enforcement mode.
6. **Backfill wave** — **groundwork only**.
   Use `reckon audit` output to identify candidate stale docs, but defer any
   fleet-scale backfill pass until milestones 1/4/5 are reviewed and approved.

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
per-plan state. `reckon-sync` symlinks `~/docs-server/state/<project>`
to `<repo>/docs/state/<project>` so the server can write this file
back to the repo.

**Any HTML file is a plan.** Existence is sufficient — a page surfaces
as `status=draft` if it has no plan markup. No `plan-status` meta
opt-in required.

**Per-stage archival under `docs/archive/`.** When work transitions
phases, write a **new** HTML file under `docs/archive/`
(`<plan>-<phase>.html`, e.g. `tokenizer-eval-locked.html`) — do not
overwrite the evergreen page. The archived file becomes the audit trail.
Per-stage files in `archive/` are excluded from the live inventory.

**HTML is the source of truth.** Do not author markdown plans under
`plans/<slug>.md` and run a generator. Auto-regeneration overwrites
per-stage history and is the anti-pattern that motivated these skills.
Any existing `plans/` markdown is read-only history — archive it under
`plans/archive/` when migrating an old project.

### Server operations

The HTTP server is `reckon serve` (the `reckon` Python package) — **one**
process listening on `127.0.0.1:8765`. Run it inside a persistent terminal
multiplexer; the canonical one on this workstation is a **zellij session
named `reckon`** (not tmux — the old `tmux -s docs-server` guidance was
stale). Config + state live under reckon's config home (currently
`~/docs-server/` — a legacy name; the rename to `~/.config/reckon/` is
tracked in the `reckon-schema-and-tooling` plan, F5).

- Start (detached, survives logout): launch inside the `reckon` zellij
  session — `uv run --project ~/Code/reckon reckon serve`
- Find it: `ss -ltnp | grep :8765` → the listening `reckon` pid
- Stop: `kill <pid>` (the pid from the line above)
- Restart: stop then start. **Server/parse/render code changes are NOT hot** —
  a running `reckon serve` (and every `reckon mcp` stdio server) holds the old
  code in memory until restarted/reconnected. Coordinate restarts (F5).
- Mounts: `<config-home>/mounts.json` — re-read on every request, no restart needed
- Plan state (GET): `curl http://127.0.0.1:8765/plan/<project>/<slug>` (parsed semantic elements + `version`)
- Plan patch (POST): `curl -X POST -H 'If-Match: <version>' -d '{"status":"shipped"}' http://127.0.0.1:8765/plan/<project>/<slug>`
- Discovery: `curl http://127.0.0.1:8765/_discover/<project>` (all plans + full parsed state)
- Project config: `curl http://127.0.0.1:8765/state/<project>/index.json`

### reckon MCP tools

The reckon MCP server exposes plan state R/W as structured tools. Claude Code
auto-starts it via the MCP config (`reckon mcp` stdio transport). All agents
working with plans MUST use these tools for state mutations — they rewrite
the plan's semantic HTML elements directly.

**Invocation** (if needed manually): `uv run --project ~/Code/reckon reckon mcp`

**The surface is three tools** (the granular mutators/reads are collapsed into
these — `read_plan` discovery + `with_schema` fold the old reads; `edit_plan`
ops fold the old writes):

| Tool | Purpose |
|------|---------|
| `read_plan` | Read + context-inject. `read_plan(project, slug)` → one plan's parsed state + `version`. `with_schema=True` adds the published JSON Schema, a dos/don'ts note, and the op-vocabulary. `read_plan(project)` (slug omitted) → DISCOVERY (plans + followups + questions + sprints + milestones + `active_sprint_id`). `read_plan()` → all mounted projects. |
| `edit_plan` | The one validated write. `edit_plan(project, slug, ops, expected_version[, create])`. `ops` is an ordered list applied to a working copy, schema-validated, then written atomically. Verbs: `set` / `append` / `resolve` / `lock` / `move`; `create=True` (with `expected_version=0`) scaffolds a new plan then applies ops. Routes `slug="index"` to project config (sprints/milestones/timeline/blockers). |
| `audit` | Schema-conformance audit of every plan in a project + index reindex (WARN/report only — never mutates). The "warn" half of reject-write-warn. Distinct from the CLI `reckon doctor`, which checks infra/skills/mounts, not schema. |

**Op vocabulary:** call `read_plan(project, slug, with_schema=True)["op_vocab"]`
for the full `edit_plan` op grammar (it inlines the set/append/resolve/lock/move
+ create rules alongside the schema).

**Mandatory rule:** always call `read_plan` first to get the current `version`
before any write — writes are rejected (412) if `version` doesn't match.

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
- **Discovery is not redirected.** `read_plan(project)` with the slug
  omitted (cross-plan inventory/followups/sprints) always scans the
  registered mounts. To read a worktree plan, name its slug explicitly with
  `checkout_path`.

**Recommended workflow for orchestrators with worktree fleets:** prefer that
the **orchestrator** (running in the main checkout) perform `index`/sprint/
followup state mutations, while worktree workers author their plan **HTML**
in their own tree and `read_plan(..., checkout_path=…)` for state. When a
worktree worker must mutate state itself, it passes `checkout_path=<its repo
root>` and commits the resulting `path` from its own tree.

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

### Mandatory prompt template (§05) on every followup

Every followup written into the plan's followups section MUST set its `prompt`
(in `<pre class="r-fu-prompt">`) to a copy-paste prompt for the next agent. The template:

```
Project: <project-name>
Plan:    <slug> (https://<published>/<slug>.html)
Section: <§ if applicable>
Tier:    <haiku | sonnet | opus>  (latest version per family)

Context
  2–3 sentences on why this is queued now and what landed before it.

State to read
  GET /plan/<project>/<slug>   (parsed plan state — decisions, followups, status, version)
  (any other plans whose state matters)

Locked decisions to honour
  <key> → <choice>
  ...

Open decisions to surface (do not resolve)
  <key>, <key>, ...

Constraints
  licence, format, environment, blockers cleared by this point

Done-when
  1. measurable artefact (commit, file, bench number)
  2. tests still green
  3. followup written into plan + this followup marked resolved
```

The reckon-edit and reckon-ship skills enforce this. A followup
without a prompt is a hard failure — the supervisor either fills it in
or rejects the worker's report.

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
- `<pre class="r-fu-prompt">` — §05 copy-paste prompt (MANDATORY)
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

### Phase suffixes on per-stage HTML

Per-stage archival files live under `docs/archive/` so they are excluded from
the live plan inventory.

| File | Location | Use when |
|---|---|---|
| `<plan>.html` | `docs/` | Evergreen — most-recent design view (always present) |
| `<plan>-draft.html` | `docs/archive/` | Initial exploration before any item moves to `active` |
| `<plan>-<section>-landed.html` | `docs/archive/` | A section's work has landed; outcomes captured |
| `<plan>-<decision>-locked.html` | `docs/archive/` | A decision is locked irreversibly |
| `<plan>-shipped.html` / `-final.html` | `docs/archive/` | Plan fully implemented |
| `<plan>-postmortem.html` | `docs/archive/` | Retrospective after shipping |

### Repo layout — one canonical format

Every repo uses the same layout:

- `docs/<slug>.html` — plan pages (any HTML file = a plan)
- `docs/archive/<slug>-<suffix>.html` — per-stage archival (excluded from inventory)
- `docs/state/<project>/index.json` — project config only (sprints, milestones, `active_sprint_id`)
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

### Model selection for plan implementation

When dispatching sub-agents for plan work, pick the model from this table.
Item metadata in `index.json` may carry a `tier` field (legacy name: `model_tier`)
that overrides the default — if present, **use what the plan says**.

| Tier | Use for | Avoid for |
|---|---|---|
| **Haiku 4.5/4.6** | Research summarisation, audit reads, formatting/typo fixes, single-file mechanical edits, file-by-file fan-out | Solver code, ambiguous scope, multi-file edits, any production write that needs judgment |
| **Sonnet 4.6** | Well-scoped Python work, test additions, docs, single-domain edits, plan items with explicit `done_when`, single-file feature work | Fortran, C++, solver-physics, cross-cutting refactors, ambiguous scope, plan synthesis, RCA |
| **Opus 4.7** | Multi-file refactors, Fortran/C++ edits, solver/physics work, RCA, strategic planning, plan synthesis, ambiguous scope | Trivial / mechanical work — wasted spend |

**Default by item category** (used when `tier`/`model_tier` is missing):

| Category match | Default tier |
|---|---|
| `research` · `audit` · `summarise` · `inventory` | haiku |
| `feature` · `test-add` · `docs` · `config` · `python` · `cli` | sonnet |
| `solver-physics` · `fortran` · `c++` · `cross-cutting-refactor` · `rca` · `plan-synthesis` · `strategy` | opus |

### Fleet patterns (canonical)

| Situation | Pattern |
|---|---|
| 1 item, scoped | One inline worker (or supervisor does it). Sonnet default. |
| 2 items, independent | Two parallel workers, one per item |
| 3 – 8 items, independent | Sonnet fleet, one per item, non-overlapping file scopes. Coordinator audits each commit. |
| > 8 items, fan-out read | Haiku reader fleet + Sonnet (or Opus) synthesiser. See efit 557-plan audit. |
| Cross-cutting / strategic | Single Opus (do not parallelise — context is the constraint) |
| Multi-file Fortran/C++ | Single Opus, or Opus fleet of ≤ 3 with very tight file scopes |

Whenever a fleet is dispatched, the **Mandatory Sub-Agent Dispatch
Preamble** in `~/.agents/AGENTS.md` (Parallel Agent Safety section)
is binding — embed it verbatim in every worker prompt.

### What goes wrong without these skills

The recorded incidents that motivated this skill set:

1. **Skill not loaded.** "Implement the X.html plan" → edited markdown
   and re-ran a generator instead of editing HTML directly. The
   reckon-* skills' descriptions are tuned so this no longer slips by.
2. **Per-stage rule missed.** Landed work appended to the evergreen
   instead of producing an archival `<plan>-<phase>.html` under
   `docs/archive/`. `reckon-edit` and `reckon-ship` enforce this.
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
6. **Wrong model tier on a fleet.** Sonnet dispatched on Fortran
   solver edits → silent regressions. The model-selection table
   above is binding; check `item.tier` (or legacy `item.model_tier`) first, default by
   category second, escalate upward when in doubt.
