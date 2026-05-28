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
| "queue followup / add note / add research / add comment" | **followup / note** |
| "resolve followup / mark followup done" + id | **followup** |
| "propose / start / close / rebalance sprint" | **sprint** |
| "move item X to sprint Y" | **sprint** |
| "archive / retire / ship the plan" | **archive** |
| `/reckon-edit <slug>` | detect from args or ask |

If the plan doesn't exist → hand off to `reckon-create`.
If the user wants to execute the work → hand off to `reckon-ship`.

## Hard rules

1. **HTML is the source of truth.** Never edit `docs/archive/` files — they are frozen.
2. **Decide evergreen-edit vs phase-transition before touching the page.**
3. **Content parity.** Add text in new blocks; do not reflow or paraphrase existing content.
4. **All plan state lives in semantic HTML elements.** Edits to decisions, followups, status,
   and impl go into `<meta name="plan-*">` scalars and `data-reckon` section elements — via
   MCP tools or `POST /plan/<project>/<slug>`. No per-plan sidecar JSON files.
5. **Every followup MUST carry a `<pre class="r-fu-prompt">` block.** See §05 template below.
6. **Locked decisions are a contract.** Use the dissent flow (write a new followup) — never silently re-lock.

```html
<!-- ❌ WRONG — stub body -->
<h2 id="s3">§3 · Implementation</h2>
<p>See state §3.</p>

<!-- ✅ CORRECT — full prose in HTML -->
<h2 id="s3">§3 · Implementation</h2>
<p>New module <code>src/preprocess.py</code> exposes …</p>
```

## State write pattern

**Write path — MCP tools (preferred):**

```
read_plan(project, slug)           → returns parsed state + version
lock_decision(project, slug, key, choice, rationale, version)
append_followup(project, slug, followup_obj, version)
resolve_followup(project, slug, id, outcome, version)
set_status(project, slug, status, version)
set_impl(project, slug, impl, version)
patch_plan(project, slug, patch_dict, version)
```

Always call `read_plan` first to get the current `version`. Writes are rejected (412) on mismatch.

**Write path — HTTP fallback:**

```bash
PROJECT="$(basename "$(git rev-parse --show-toplevel)")"
SLUG="<slug>"
PLAN=$(curl -s "http://127.0.0.1:8765/plan/$PROJECT/$SLUG")
CUR_VER=$(echo "$PLAN" | jq -r '.version // 0')
curl -s -X POST \
  -H 'Content-Type: application/json' \
  -H "If-Match: $CUR_VER" \
  -d '{"decisions.scan-strategy.choice": "glob", "decisions.scan-strategy.rationale": "simpler", "decisions.scan-strategy.when": "2026-05-26", "decisions.scan-strategy.by": "smc"}' \
  "http://127.0.0.1:8765/plan/$PROJECT/$SLUG"
```

Patch body is a **flat map of dotted keys**. `version` is server-owned — never include in a patch. Server bumps `version` and sets `modified` on every successful write.

## Intent: prose edit

| Edit intent | Action | Output |
|---|---|---|
| Typo, clarification, new subsection | **Evergreen** | Edit `docs/<slug>.html` in place |
| Followup, note, question | **Structured write** | POST patch or MCP call |
| Decision with rationale | **Structured write** | `lock_decision` or POST patch |
| Section fully landed | **Phase transition** | Write `docs/archive/<slug>-<n>-landed.html`; append summary to evergreen |
| Decision locked irreversibly | **Phase transition** | Write `docs/archive/<slug>-<key>-locked.html` |
| Plan fully implemented | **Phase transition + archive** | See §archive below |

**Evergreen steps:**
1. Read `docs/<slug>.html`.
2. Make the smallest edit; add new `<h3>` blocks rather than modifying existing paragraphs.
3. Update `modified` via POST patch (`{"modified": "YYYY-MM-DD"}`).
4. Suggest commit: `docs(<slug>): <short summary>`. Do not commit unless asked.

**Phase transition steps:**
1. Determine suffix: `landed`, `locked`, `final`, or section-specific (`02-landed`).
2. Create `docs/archive/<slug>-<suffix>.html` — frozen snapshot with link back to evergreen.
3. Append a short `<h3>` block in the evergreen pointing at the archive file.
4. Update `modified` via POST patch.

## Intent: decisions

Decisions are `<div class="r-dec" data-key="…">` inside `<section data-reckon="decisions">`.
Open: `data-choice=""`. Locked: `data-choice="<answer>"` with `data-by`/`data-when` set.
Options are `<button class="r-opt" data-value="…">` — chosen button gets class `chosen`.
Free-form decision: no `<button>` elements; `data-choice` holds the typed answer.

**Lock a decision:**
1. Read state via `read_plan` or `GET /plan/<project>/<slug>` — get `version`.
2. POST dotted patch: `decisions.<key>.choice`, `.rationale`, `.when`, `.by`.
3. If irreversible, write archival `docs/archive/<slug>-<key>-locked.html`.

**Interactive walkthrough:**
1. Collect open decisions (where `choice == ""`); present each `r-dec-q` + options.
2. Accumulate choices; POST a single patch with `If-Match` at the end.
3. Report how many decisions were locked.

**Dissent / reopen:**
1. Write a followup (`recommends_skill: "/reckon-edit <slug> --reopen <key>"`).
2. Body: locked choice, what evidence changed, what you propose.
3. Never silently re-lock.
4. On `--reopen`: snapshot into `docs/archive/<slug>-<key>-locked.html`; send a patch clearing `decisions.<key>.choice`.

## Intent: followup / notes / research / comments

**Append a followup:**
1. Read state; generate `id: "f-<unix-secs-base36>"`.
2. POST via MCP `append_followup` or patch. A followup without `prompt` is a hard failure.

Minimum followup object:
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
  "prompt":           "<§05 template body — mandatory>"
}
```

**Resolve a followup:** MCP `resolve_followup` or patch `resolved_at`, `resolved_by`, `outcome`.

**Research items:** append via MCP `add_research`; `prompt` not required.

**Comments:** The SPA creates comments when a user selects text — a "¶ Comment" button appears, clicking it opens a popover. The comment is stored under the nearest `h2[id]` anchor (`data-section`). When reading a plan, check `<section data-reckon="comments">` for human feedback. Comments are agent-readable; respond to them in the next followup or prose edit.

## §05 followup template

Every followup `prompt` MUST be built from this template:

```
Project: <project-name>
Plan:    <slug> (http://localhost:8765/<project>/<slug>.html)
Section: <§ if applicable>
Tier:    <haiku | sonnet | opus>

Context
  2–3 sentences on why this is queued now.

State to read
  GET /plan/<project>/<slug>   (decisions, followups, status, version)

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

## Intent: sprint management

Operates only on `docs/state/<project>/index.json`. Does NOT dispatch workers (use `reckon-ship`).

**Propose a sprint:**
1. Discover plans via `GET /_discover/<project>`.
2. Filter `status in {active, pending}`.
3. Score: `roi × effort_inverse × milestone_priority`. Apply dependency-aware ordering.
4. Partition into N sprints; each item:
   ```json
   {"slug": "my-plan", "why_now": "Highest ROI; gates M1", "tier": "sonnet", "done_when": "tests green"}
   ```
5. Print proposal; ask to confirm before writing `index.json`.

**Start sprint:** `status: "planned"` → `"active"`. Warn if another is active.
**Close sprint:** `status: "done"`. List unshipped items; ask how to handle.
**Move item:** Remove from source `items[]`, append to target.
**Rebalance:** Re-score; diff against current; ask to confirm before applying.

## Intent: archive

1. Write `docs/archive/<slug>-final.html` — frozen snapshot.
2. Move `docs/<slug>.html` to `docs/archive/<slug>.html`.
3. Update `docs/index.html` if present.
4. POST `status: "archived"`.

## Cross-references

- `reckon-create` — scaffold a new plan.
- `reckon-ship` — execute the work a plan describes.
- `reckon-status` — read-only inspection.
- `reckon-sync` — register a project mount and seed shared assets.
- `~/Code/reckon/PLAN-FORMAT.md` — canonical format (semantic HTML, endpoints).
