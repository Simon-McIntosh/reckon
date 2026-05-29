# RFC-001 — Collapse the reckon MCP tool surface; let skills own authoring

Status: proposed (2026-05-29)
Author: agent/opus (with Simon McIntosh)
Scope: `reckon/mcp.py` tool surface + the `reckon-*` skills. Shared across
imas-ambix, imas-efit, imas-codex (one pip-installed reckon server).

## The question

Do we keep ~19 granular MCP plan tools, pivot entirely to skills + direct HTML
editing, or something between? The current set feels both *too many* and
*functionally limited* (no `create_plan`, no `create_sprint` — the latter is a
wall you hit when opening a sprint).

## What the code actually shows

- **The 19 tools are one shape.** Every mutator in `mcp.py` is
  `read dict → mutate one nested path → write_plan(version-checked)`, a thin
  preset over **5 general store primitives** (`read_plan`, `write_plan`,
  `patch_plan`, `set_nested`, `append_to_list`, `resolve_in_list`).
- **The store is already general — and already supports "create".**
  `_write_state` creates a plan when `expected_version==0` (stub HTML + state
  injection); `patch_plan(project,"index",{"sprints":[…]})` can create a sprint.
  So `create_plan`/`create_sprint` are *surface* gaps, not store gaps.
- **Prose vs structured state is already cleanly split.** The free-text body
  (`<h2>/<p>` sections — the bulk of a plan) is authored/edited directly in HTML
  by the skills; tools never touch it. The tools only mutate the *structured*
  `data-reckon` sections (decisions/followups/comments/sprints + head meta),
  via a dict↔HTML round-trip in `_plan_html` that **guarantees schema-correct
  HTML**.
- **The live dashboard re-discovers from HTML** (`_list_plans`/`discover_plans`),
  so `index.json` rollups are a cache, not the source of truth — a stale rollup
  does not break the live view.

## What genuinely needs a tool (i.e. can't be "just edit the HTML")

1. **Concurrency.** Parallel agents (this project runs fleets) need
   version-checked atomic writes; raw concurrent HTML edits lose updates.
2. **Schema-correct structured-state HTML.** The dict→HTML round-trip prevents
   malformed `data-reckon` elements that the SPA can't parse. Hand-writing
   `data-key`/`data-status`/IDs by hand is error-prone.
3. **Auto-stamping** (`when`/`resolved_at`/ids) and light field validation.
4. **Reindex** (refresh `index.json` rollups) — nice-to-have, not load-bearing.

Everything else — all prose, structure, new sections — the LLM authors directly
in HTML and is good at it. That part should be skill-driven, not tool-driven.

## Recommendation: collapse 19 → 4, skills own authoring

| Keep / add | Tool | Replaces | Why a tool |
|---|---|---|---|
| **read/context** | `read_plan(project, slug?)` — returns parsed state + version + (NEW) the schema/dos-don'ts inline; `slug` omitted → project discovery | read_plan, list_plans, list_projects, list_sprints, list_followups, list_questions | a *prompt-injector*: one call gives state + version + the rules to edit correctly |
| **universal write** | `edit_plan(project, slug, ops[], expected_version)` — small op vocabulary: `set` (path=value), `append` (list+item, auto-id/stamp), `resolve` (list,id,fields), `lock` (decision); `slug="index"` ops cover sprints/inventory; **`create=true`/`expected_version=0` creates** | patch_plan, append_comment, lock_decision, append_followup, resolve_followup, set_status, set_impl, resolve_question, add_research, update_sprint, add_sprint_item, move_sprint_item, update_inventory_item, **+create_plan, +create_sprint** | the one boundary that earns its keep: version-check + schema-correct HTML render + stamping, over the existing store primitives |
| **maintenance** | `doctor(project)` — validate every plan's HTML round-trips + reindex `index.json` rollups | (new; partial logic in cli/test_doctor) | catch malformed state; refresh the cache |
| (optional) | keep a thin `read_plan`-discovery mode instead of a separate `list` | — | — |

Net: **~4 tools** (read/context, edit_plan, doctor, +discovery-as-a-read-mode),
down from 19. The skills (`reckon-create/edit/ship/status`) become the
**authoring guidance** — schema, worked examples, dos/don'ts, workflow — and do
all prose work via direct HTML; `edit_plan` is the safe boundary for structured
state.

### Answering the framing questions
- *Should we have any tools?* Yes, but **minimal** — only for the four
  capabilities above. In a single-agent world even those could be skills; the
  fleet/concurrency reality justifies one version-checked write boundary.
- *Tools as prompt-injectors?* Exactly right for the **read** side: `read_plan`
  should inject state + version + schema/dos-don'ts so the LLM can edit HTML
  correctly. The write side stays a real (thin) tool for safety.
- *Too many / limited?* Both true — they're over-decomposed presets with two
  real gaps (create). Collapsing to a small op-vocabulary write fixes both.

## Migration (non-breaking → deprecate → remove)

1. **Add (additive, non-breaking):** `create_plan`, `create_sprint`, `doctor`
   now — fill the gaps + reindex, reusing existing store primitives. *(implemented
   alongside this RFC — see commit.)*
2. **Add `edit_plan`** (op vocabulary) alongside the 15 mutators; enrich
   `read_plan` to inject schema. Tests in `tests/test_mcp_tools.py`.
3. **Deprecate** the 15 granular mutators (keep as aliases that delegate to
   `edit_plan`); update the `reckon-*` skills to use `read_plan` + `edit_plan`
   only, with schema + dos/don'ts + examples carrying the authoring load.
4. **Validate** across a real multi-repo session, then **remove** the aliases.

MCP tool changes take effect on server restart (next session), so steps land
safely out-of-band of any running fleet.
