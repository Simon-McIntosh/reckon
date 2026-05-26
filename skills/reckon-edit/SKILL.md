---
name: reckon-edit
description: >-
  Edit an existing plan or manage the central index — prose edits, per-stage
  files, decision locking, followup creation (§05 template), sprint management,
  and plan archiving. Decides between evergreen edit vs new stage file for phase
  transitions. Trigger verbs: "update / amend / record / add to / revise /
  lock decision / resolve decisions / queue followup / propose sprint / start
  sprint / close sprint / rebalance / archive / retire the plan /
  /reckon-edit <slug>". For new plans use reckon-create; for executing work use
  reckon-ship; for read-only inspection use reckon-status.
allowed-tools: Read Write Edit Bash(*) Grep
---

# reckon-edit — mutations to existing plans and the central index

## When to invoke

| Signal | Intent path |
|---|---|
| "update / amend / revise / add section" | **prose** |
| "lock / record / resolve decision" + decision key | **decision** |
| "walk me through open decisions" | **decision** (interactive) |
| "queue followup / add note / add research" | **followup** |
| "resolve followup / mark followup done" + id | **followup** |
| "propose / start / close / rebalance sprint" | **sprint** |
| "move item X to sprint Y" | **sprint** |
| "archive / retire / ship the plan" | **archive** |
| `/reckon-edit <slug>` | detect from args or ask |

If the plan doesn't exist → hand off to `reckon-create`.
If the user wants to execute the work → hand off to `reckon-ship`.

## Hard rules

1. **HTML is the source of truth.** Never edit `docs/archive/` files — they are frozen.
2. **Decide evergreen-edit vs phase-transition before touching the page.** See §prose below.
3. **Content parity.** Add text in new blocks; do not reflow or paraphrase existing content.
4. **All plan state lives in the plan HTML's island.** Edits to decisions, followups, status,
   and impl go into the `<script type="application/json" id="reckon-state">` block — via
   MCP tools or `POST /plan/<project>/<slug>`. There is no per-plan sidecar JSON.
5. **Every followup MUST carry a `prompt` field.** A followup without `prompt` is a hard failure.
6. **Locked decisions are a contract.** Use the dissent flow (write a new followup) — never silently re-lock.
7. **Never write stubs that cross-reference state.** If a section body is missing,
   write the actual prose in the HTML — do not write `<p>See state §N for details</p>`.
   The island has no `sections[]` field. Plan body is always in HTML.

```html
<!-- ❌ WRONG — stub with cross-reference -->
<h2 id="s3">§3 · Implementation</h2>
<p>See state §3.</p>

<!-- ✅ CORRECT — full prose in HTML -->
<h2 id="s3">§3 · Implementation</h2>
<p>New module <code>src/preprocess.py</code> exposes...</p>
```

## State write pattern

The plan HTML is the sole store. All structured state lives in the island:

```html
<script type="application/json" id="reckon-state">
{ ...canonical plan data... }
</script>
```

**Write path — MCP tools (preferred):**

```
read_plan(project, slug)           → returns island + version
lock_decision(project, slug, key, choice, rationale, version)
append_followup(project, slug, followup_obj, version)
resolve_followup(project, slug, id, outcome, version)
set_status(project, slug, status, version)
set_impl(project, slug, impl, version)
patch_plan(project, slug, patch_dict, version)
```

Always call `read_plan` first to get the current `version` before any write.
Writes are rejected (412) if `version` doesn't match.

**Write path — HTTP (when MCP unavailable):**

```bash
PROJECT="$(basename "$(git rev-parse --show-toplevel)")"
SLUG="<slug>"

# 1. Read island + version
ISLAND=$(curl -s "http://127.0.0.1:8765/plan/$PROJECT/$SLUG")
CUR_VER=$(echo "$ISLAND" | jq -r '.version // 0')

# 2. Build dotted patch (flat key → value)
# Example: lock a decision
PATCH='{"decisions.scan-strategy.choice": "glob", "decisions.scan-strategy.rationale": "simpler", "decisions.scan-strategy.when": "2026-05-26", "decisions.scan-strategy.by": "smc"}'

# 3. POST with If-Match
curl -s -X POST \
  -H 'Content-Type: application/json' \
  -H "If-Match: $CUR_VER" \
  -d "$PATCH" \
  "http://127.0.0.1:8765/plan/$PROJECT/$SLUG"
```

Rules:
- Always read `version` before posting (`GET /plan/<project>/<slug>`).
- The patch body is a **flat map of dotted keys** (not the full island).
- A missing `If-Match` header causes HTTP 412. A version mismatch returns 412 with `{current_version, current_data}` — rebase and retry.
- Server bumps `version` on every successful write and sets `modified` to today.
- `version` is server-owned — never include it in a patch.
- Per-stage archival files live under `docs/archive/` — not `docs/`.

## Intent: prose edit

**Evergreen vs stage-file decision:**

| Edit intent | Treat as | Output |
|---|---|---|
| Typo, broken link, small clarification, new subsection | **Evergreen** | Edit `docs/<slug>.html` in place |
| Adding a followup, note, or question | **Island write only** | POST patch or MCP call; usually no HTML body change |
| Recording a decision with rationale | **Island write** | POST patch or MCP `lock_decision`; no HTML body change |
| Section fully landed; work is done | **Phase transition** | Write `docs/archive/<slug>-<n>-landed.html`; append summary to evergreen |
| Decision locked irreversibly | **Phase transition** | Write `docs/archive/<slug>-<key>-locked.html` |
| Plan fully implemented | **Phase transition + archive** | See §archive below |

**Steps (evergreen):**
1. Read `docs/<slug>.html` end to end.
2. Make the smallest edit that achieves the goal; add new `<h3>` blocks rather than modifying existing paragraphs.
3. Update `modified` in the island (via POST patch) to today.
4. Suggest commit: `docs(<slug>): <short summary>`. Do not commit unless asked.

**Steps (phase transition):**
1. Determine suffix: `landed`, `locked`, `final`, or section-specific (e.g. `02-landed`).
2. Create `docs/archive/<slug>-<suffix>.html` — frozen snapshot with same CSS links, link back to evergreen.
3. Append a short `<h3>` block in the evergreen pointing at the new file.
4. Update `modified` in the island.

## Intent: decisions

`decisions` is a **map** in the island, keyed by decision key:

```json
"decisions": {
  "scan-strategy": {
    "title":    "How should scanning work?",
    "context":  "…",
    "choices":  ["glob", "index"],
    "choice":   "glob",
    "rationale": "…",
    "when":     "2026-05-26",
    "by":       "smc"
  }
}
```

**Lock a single decision:**
1. Read island via `read_plan` (MCP) or `GET /plan/<project>/<slug>` (HTTP).
2. POST a dotted patch: `decisions.<key>.choice`, `decisions.<key>.rationale`, `decisions.<key>.when`, `decisions.<key>.by`.
3. If the decision is irreversible, follow the phase-transition rule (write an archival `docs/archive/<slug>-<key>-locked.html`).

**Interactive walkthrough ("walk me through all open decisions"):**
1. Read island; collect `decisions` keys where `choice` is `""` or null.
2. For each open decision, present the `title`, `context`, and `choices`; ask the user to choose.
3. Accumulate choices; send a single POST patch with If-Match at the end.
4. Report how many decisions were locked.

**Dissent / reopen flow:**
1. Write a followup (§05 template, `recommends_skill: "/reckon-edit <slug> --reopen <key>"`).
2. Body: what the locked choice was, what evidence changed, what you propose instead.
3. Never silently re-lock. A human or coordinator reviews and accepts or rejects the followup.
4. On `--reopen`: snapshot the existing decision into `docs/archive/<slug>-<key>-locked.html`, send a patch that clears `decisions.<key>.choice`, add a "reopened" badge to the HTML row, append a `notes[]` entry.

## Intent: followup / notes / research

**Append a followup:**
1. Read island; generate `id: "f-<unix-secs-base36>"`.
2. Build the followup object (minimum body below).
3. POST via MCP `append_followup` or `POST /plan/<project>/<slug>` with a patch that appends to `followups`.
4. A followup without `prompt` is a hard failure — use the §05 template.

Minimum followup body:
```json
{
  "id":               "f-<base36>",
  "status":           "open",
  "written_by":       "<agent | smc>",
  "written_at":       "YYYY-MM-DD HH:MM",
  "title":            "<imperative short title>",
  "body":             "<2–3 sentences of context>",
  "recommends_skill": "/reckon-ship <slug> | /reckon-edit <slug> | null",
  "tier":             "haiku | sonnet | opus",
  "est_turns":        "~1h | ~1d | ~1 sprint",
  "prompt":           "<§05 template body — mandatory>"
}
```

**Resolve a followup by id:**
1. Read island; find entry in `followups[]` by `id`.
2. Via MCP `resolve_followup` or a patch: set `resolved_at`, `resolved_by`, `outcome`, `status: "resolved"`.

Notes and research items use the same append pattern; `prompt` is optional for those.

## Intent: sprint management

Operates only on `docs/state/<project>/index.json` (project config: sprints, milestones, `active_sprint_id`).
Does NOT dispatch workers (that is `reckon-ship`).
Does NOT read plan HTML content (that is `reckon-ship` / `reckon-status`).

**Propose a sprint:**
1. Discover plans via `GET /_discover/<project>` — each plan carries its full island state.
2. Filter by `status in {active, pending}`.
3. Score: `roi (high=3, mid=2, low=1) × effort_inverse (S=4, M=3, L=2, XL=1) × milestone_priority`.
4. Apply dependency-aware ordering (blocked items trail their blockers).
5. Partition into N sprints (default 4); each item as an object:
   ```json
   {"slug": "my-plan", "why_now": "Highest ROI; gates M1", "tier": "sonnet", "done_when": "tests green"}
   ```
   Use `why_now` (not `justification`). Use `tier` (not `model_tier`).
6. Print proposal as a table; ask user to confirm before writing.
7. Write updated `index.json` with the new sprints array.

**Start a sprint:** Promote `status: "planned"` → `"active"`. Warn if another sprint is already active.

**Close a sprint:** Set `status: "done"`. List any unshipped items; ask how to handle (roll, abandon, or keep open).

**Move item:** Remove from source `items[]`, append to target `items[]`. Write with optimistic concurrency.

**Rebalance:** Re-score + diff against current `sprints[]`; surface proposed moves; ask to confirm before applying.

State path: `docs/state/<project>/index.json`.

## Intent: archive

1. Write `docs/archive/<slug>-final.html` — frozen snapshot with final status grid, all locked decisions, implementation commit SHAs, and a link to the evergreen.
2. Move `docs/<slug>.html` to `docs/archive/<slug>.html`.
3. Update `docs/index.html` plans table: move the row to "Completed" or drop it.
4. Remove from active `<nav>` or add an "(archived)" marker.
5. POST `status: "archived"` and `modified` to the plan's island endpoint.

## §05 followup template

Every followup `prompt` field MUST be built from this template:

```
Project: <project-name>
Plan:    <slug>
Section: <§ if applicable>
Tier:    <haiku | sonnet | opus>

Context
  2–3 sentences on why this is queued now.

State to read
  GET /plan/<project>/<slug>   (island — decisions, followups, status)

Locked decisions to honour
  <key> → <choice>

Open decisions to surface (do not resolve)
  <key>, <key>

Constraints
  <licence, format, environment>

Done-when
  1. <measurable artefact>
  2. tests still green
  3. followup written + this followup marked resolved
```

## Cross-references

- `reckon-create` — scaffold a new plan.
- `reckon-ship` — execute the work a plan describes.
- `reckon-status` — read-only inspection of plan and sprint state.
- `reckon-sync` — register a project mount and seed shared assets.
- `~/Code/reckon/PLAN-FORMAT.md` — canonical format (island schema, endpoints, what is gone).
- `~/Code/reckon/` — docs-server source; CSS lives in `docs/_shared/`.
- `~/docs-server/mounts.json` — mount registry (no restart needed).
