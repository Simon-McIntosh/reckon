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

## HTML-first plans

Plain markdown remains fine for short prose (READMEs, brief notes).
Anything project-level that needs tables, diagrams, status tiles,
side-by-side comparisons, interactive decision capture, or shared
state across humans and agents is a **plan**, and plans are HTML.

### Skills you must use — never freelance

Plans have a specific skill set. Do not edit `docs/*.html`
without invoking the matching skill first; the skills bake in rules
that ad-hoc edits routinely violate (per-stage archival, state
writeback, content parity, fleet safety).

| Intent | Skill | Slash command |
|---|---|---|
| Create a brand-new plan | `reckon-create` | `/reckon-create <slug>` |
| Edit an existing plan, lock a decision, record an outcome, write a followup, manage sprints | `reckon-edit` | `/reckon-edit <slug>` |
| Implement the work a plan describes; record outcomes; followup with §05 prompt | `reckon-ship` | `/reckon-ship <slug> [section]` |
| Pure-read inspection across all plans in this repo | `reckon-status` | `/reckon-status` |
| Set up or refresh reckon infra in a repo (CSS, mounts, state dir, symlink) | `reckon-sync` | `/reckon-sync` |
| Non-plan dynamic docs (reviews, explainers, dashboards) | `html-docs` | — |

**Trigger discipline.** When the user says "the X plan", read the
relevant skill's SKILL.md **before** touching any file. The skills
live at `~/.claude/skills/reckon-*/SKILL.md`. They are short; reading
them is cheap.

### Plan-state integrity (mandatory — fix for the "silent bypass" failure mode)

**Failure mode that motivated this section.** 2026-05-21: shipped
plans-infra in dotfiles + ambix without updating the plan's state
JSON to reflect that it shipped. The plan-system told a different
story than the codebase. RCA: the coordinator authored a custom
sub-agent dispatch prompt instead of routing through `/reckon-ship`
(which has the followup-write requirement baked in), AND failed to
write a closing followup on the parent plan when work completed.

**The mandate.** Any change to a plan's state — implementation lands,
decision resolves, status changes, blocker clears, sprint moves —
MUST be reflected in `docs/state/<project>/<slug>.json` (and
`docs/state/<project>/index.json` for inventory rollups) **in the
same turn that the change happens**.

Concretely:

1. **When work lands on a plan**:
   - `data.status` updated (`active` → `shipped`/`done` when fully
     done; `active` → `blocked` when stalled; etc.)
   - `data.impl` advanced toward 1.0
   - `data.last_modified` set to now
   - **The driving followup MUST be resolved** with `resolved_at`,
     `resolved_by`, `outcome` describing what landed
   - **A new followup MUST be written** with the §05 prompt template
     for whatever comes next — or with `outcome: "done — no followup"`
     when the chain truly closes
   - `data._version` will be incremented by the docs-server POST (do
     not set it client-side)

2. **When a decision is resolved**:
   - `data.decisions[<key>]` updated with `choice`, `rationale`, `when`,
     `by` — via `/reckon-edit` or a live POST to
     `/state/<project>/<slug>`. Direct file edits are permitted ONLY
     if you announce the bypass reason in your reply.

3. **Coordinator dispatch contract.** When dispatching a Sonnet (or
   any) worker to implement plan work, the dispatch prompt MUST:
   - Include the **§05 prompt template** with locked decisions, open
     decisions to surface, and a Done-when list
   - **Require the worker to append a followup** to the relevant
     plan's `state.followups[]` before declaring done
   - **Require the worker to resolve its driving followup** (if it
     was dispatched from one) with `resolved_at`, `outcome`

   When all workers land, the **coordinator MUST verify the followup
   landed** before marking the plan-task as shipped. If a worker
   omitted the followup, the coordinator writes it themselves
   (reflecting what the worker reported).

4. **Eat-the-dog-food check.** Before marking any reckon-ship work
   "done" in chat, verify the plan-system itself reflects the work:
   - `curl /state/<project>/<slug>.json` → `data.status` matches
     reality
   - The driving followup is resolved
   - A next followup or `done — no followup` is present
   - `data._version` has incremented
   If any of those is false, you bypassed the skill. Fix it before
   moving on.

5. **State-bypass announcement.** Any agent that edits a state JSON
   without invoking `/reckon-edit` or `/reckon-ship` MUST announce
   "bypassing /reckon-edit because X" in their reply. Silent bypasses
   are exactly the failure mode being prevented here.

This is enforced by discipline, not by tooling — but the discipline
is binding. Surface a violation explicitly when you spot one.

### Where docs live and how the server works

Every project keeps its plans under `<repo>/docs/`. The
host-wide `docs-server` at `127.0.0.1:8765` serves them under stable
URL prefixes (`http://localhost:8765/<project>/`) and exposes a JSON
state store at `/state/<project>/<doc>` that both the browser and
agents read/write.

**State files live in the repo.** `reckon-sync` symlinks
`~/docs-server/state/<project>` to
`<repo>/docs/state/<project>` so the docs-server reads and
writes the same files git tracks. One source of truth.

**Per-stage, not per-update.** When work transitions phases, write a
**new** HTML file (`<plan>-<phase>.html`, e.g.
`tokenizer-eval-locked.html`) — do not overwrite the evergreen page.
The old file becomes the audit trail. This rule is enforced by
`reckon-edit` and `reckon-ship`.

**HTML is the source of truth.** Do not author markdown plans under
`plans/<slug>.md` and run a generator. Auto-regeneration overwrites
per-stage history and is the anti-pattern that motivated these skills.
Any existing `plans/` markdown is read-only history — archive it under
`plans/archive/` when migrating an old project.

### Server operations

- Start (login node, persistent in tmux):
  `tmux new -d -s docs-server 'uv run --project ~/Code/reckon reckon serve'`
- Stop: `tmux kill-session -t docs-server`
- Mounts: `~/docs-server/mounts.json` — re-read on every request, no
  restart needed
- State endpoint:
  `curl http://127.0.0.1:8765/state/<project>/<doc>` (GET) /
  `-X POST` with JSON body (write)
- State disk path: `~/docs-server/state/<project>/<doc>.json` (which
  is the repo's `docs/state/<project>/<doc>.json` via symlink)

### reckon MCP tools

The reckon MCP server exposes plan state R/W as structured tools. Claude Code
auto-starts it via the MCP config (`reckon mcp` stdio transport). All agents
working with plans MUST use these tools for state mutations — never edit state
JSON files directly.

**Invocation** (if needed manually): `uv run --project ~/Code/reckon reckon mcp`

**Core tools:**

| Tool | Purpose |
|------|---------|
| `list_projects` | List all mounted projects |
| `list_plans` | List plans for a project |
| `read_plan` | Read current plan state + `_version` |
| `patch_plan` | Bulk-update plan fields |
| `set_status` | Update `data.status` |
| `set_impl` | Update `data.impl` (0.0–1.0) |
| `lock_decision` | Record a resolved decision |
| `append_followup` | Add a followup item |
| `resolve_followup` | Mark a followup done with outcome |
| `append_comment` | Add a note/comment |
| `list_followups` | List open followups |
| `list_questions` | List open questions |
| `resolve_question` | Mark a question resolved |
| `add_research` | Attach a research item |

**Mandatory rule:** always call `read_plan` first to get the current `_version`
before any write — writes are rejected if `expected_version` doesn't match.

**Further detail:** `GET http://127.0.0.1:8765/state/<project>/<slug>.json`
for current state, or `/_discover/<project>` for plan inventory. MCP tool
descriptions are self-documenting — query them via the MCP inspector if needed.

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
decisions doc. Each plan page can have a state.js-wired
`<table id="decisions-table">` whose rows POST to
`/state/<project>/<doc>` on click. A separate `decisions.html` is a
**read-only aggregator** that fetches state from each owning page
and shows current choices — it never writes.

### Mandatory prompt template (§05) on every followup

Every followup written into `data.followups[]` MUST set its `prompt`
field to a copy-paste prompt for the next agent. The template:

```
Project: <project-name>
Plan:    <slug> (https://<published>/<slug>.html)
Section: <§ if applicable>
Tier:    <haiku | sonnet | opus>  (latest version per family)

Context
  2–3 sentences on why this is queued now and what landed before it.

State to read
  docs/state/<project>/<plan>.json
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
  3. followup written + this followup marked resolved
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

### Per-plan state schema (additive — old files keep working)

State files at `docs/state/<project>/<doc>.json` carry an envelope
wrapping the canonical `data` payload:

```json
{
  "updated": "<iso>",
  "project": "<project>",
  "doc":     "<slug>",
  "data": {
    "status":    "active | pending | blocked | shipped | draft",
    "tier":      "haiku | sonnet | opus",
    "decisions": { "<key>": { "choice", "rationale", "when", "by" } },
    "notes":     [{ "id", "who", "bot", "when", "body", "quote?" }],
    "followups": [{ "id", "written_by", "written_at", "title", "body",
                    "recommends_skill", "touches", "blocked_by?", "tier?",
                    "est_turn", "prompt",
                    "resolved_at?", "resolved_by?", "outcome?" }],
    "research":  [{ "id", "type", "title", "source", "added_by", "when", "url" }],
    "questions": [{ "id", "section", "body", "opened_by", "opened_at", "resolved_at?" }],
    "tests":     [{ "name", "pass", "fail", "pulse", "fail_now?" }]
  }
}
```

The pre-redesign files have decision keys at the top of `data`
(without the nested `decisions` map) — `state.js` reads both shapes
via `getDecisions(blob)`.

### 3-layer CSS architecture

Pages load three stylesheets in cascade order:

1. `_shared/foundation.css` — design tokens, body, shell, primitives
   (universal; copy of the dotfiles canonical)
2. `_shared/dashboard.css` — plan-domain widgets (status, kanban,
   milestone stepper, NEXT card; copy of the dotfiles canonical)
3. `assets/project.css` — per-project overrides (optional)

Canonical source lives in `~/Code/reckon/docs/_shared/`.
The reckon server exposes them at `/_shared/<file>` (canonical, live from reckon
install) and `/_ui/<file>` for JSX components. PROJECT PAGES served by the live
server link to these absolute routes. For GitHub Pages static deployment, run
`reckon sync` to copy CSS to `docs/_shared/`, or `reckon build` for a fully
self-contained static bundle including JSX.

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

| Suffix | Use when |
|---|---|
| `<plan>.html` | Evergreen — most-recent design view (always present) |
| `<plan>-draft.html` | Initial exploration before any item moves to `active` |
| `<plan>-<section>-landed.html` | A section's work has landed; outcomes captured |
| `<plan>-<decision>-locked.html` | A decision is locked irreversibly |
| `<plan>-shipped.html` / `-final.html` | Plan fully implemented |
| `<plan>-postmortem.html` | Retrospective after shipping |

### Two layouts, both supported

The skills handle both shapes a repo can take:

**Per-doc layout** (small/young repos, e.g. imas-ambix)
- One HTML per plan, decisions live on the owning page, state at
  `state/<project>/<slug>.json`. No central aggregator beyond the
  read-only `decisions.html`.

**Central-index layout** (larger repos with many plans, e.g. imas-efit)
- Single `state/<project>/index.json` with the canonical schema
  (`plans[]`, `sprints[]`, `milestones[]`, `blockers[]`,
  `questions[]`). Multiple HTML views render from it. All plan HTMLs
  live flat at `docs/<slug>.html`.
- `reckon-create` adds `index.html`, `sprints.html`, `milestones.html`,
  `blockers.html`, `questions.html`, `inventory.html` from
  `~/.claude/skills/html-docs/templates/dashboard-*.html`.

The skills detect which model a repo uses by checking for
`state/<project>/index.json`. If present and conformant → central
model. Otherwise → per-doc.

### `index.json` canonical schema (central-index layout)

```json
{
  "data": {
    "audit_date": "YYYY-MM-DD",
    "audit_method": "human description",
    "counts": {
      "total": int,
      "status":    { "active": int, "pending": int, ... },
      "milestone": { "M0": int, "M1": int, ... },
      "roi":       { "high": int, "mid": int, "low": int },
      "effort":    { "S": int, "M": int, "L": int, "XL": int }
    },
    "plans": [{
      "slug": "<slug>", "path": "docs/<slug>.html",
      "title": "...", "status": "active",
      "milestone": "M2", "roi": "high", "effort": "M",
      "implementation_fraction": 0.0,
      "tier": "sonnet",
      "summary": "one-line synopsis",
      "last_modified": "YYYY-MM-DD",
      "evidence": ["commit SHAs"]
    }],
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
    "blockers": [{
      "id": "...", "summary": "...", "origin_path": "...",
      "blocks_n": int, "owner": "...", "next_step": "...",
      "tier": "opus"
    }],
    "questions": [{
      "key": "...", "question": "...", "options": [...],
      "context": "...", "origin_path": "..."
    }]
  }
}
```

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
   instead of producing a `<plan>-<phase>.html`. `reckon-edit` and
   `reckon-ship` enforce this.
3. **State file never written.** Decisions discussed in chat never
   reached `~/docs-server/state/`. `reckon-edit` requires the POST.
4. **`git commit -a` swept peer fleet edits.** Multiple workers,
   non-overlapping scopes, but `-a` swept WIP into the wrong commit.
   `reckon-ship`'s dispatch boilerplate explicitly bans `-a`/`-am`
   and `git add -A/./*` in every worker prompt.
5. **Decisions baked into a generator.** Centralised decision list in
   a Python script meant editing the script to add a decision. The
   new model: decision rows live on the owning plan page, written
   directly into the HTML by `reckon-edit`.
6. **Wrong model tier on a fleet.** Sonnet dispatched on Fortran
   solver edits → silent regressions. The model-selection table
   above is binding; check `item.tier` (or legacy `item.model_tier`) first, default by
   category second, escalate upward when in doubt.
