---
name: reckon-edit
description: >-
  Edit an existing plan or manage the central index — prose edits, per-stage
  files, decision locking, followup creation (§05 template), sprint management,
  and plan archiving. Decides between evergreen edit vs new stage file for phase
  transitions. Handles both per-doc and central-index layouts. Trigger verbs:
  "update / amend / record / add to / revise / lock decision / resolve decisions /
  queue followup / propose sprint / start sprint / close sprint / rebalance /
  archive / retire the plan / /reckon-edit <slug>". For new plans use reckon-create;
  for executing work use reckon-ship; for read-only inspection use reckon-status.
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
4. **A resolved decision MUST reach the state file.** HTML edits alone are insufficient.
5. **Every followup MUST carry a `prompt` field.** A followup without `prompt` is a hard failure.
6. **Locked decisions are a contract.** Use the dissent flow (write a new followup) — never silently re-lock.
7. **Never write stubs that cross-reference state JSON.** If a section body is missing,
   write the actual prose in the HTML — do not write `<p>See state §N for details</p>`.
   State JSON `data` has no `sections[]` field. Plan body is always in HTML.

```html
<!-- ❌ WRONG — stub with cross-reference -->
<h2 id="s3">§3 · Implementation</h2>
<p>See <a href="state/project/plan.json#s3">state §3</a>.</p>

<!-- ✅ CORRECT — full prose in HTML -->
<h2 id="s3">§3 · Implementation</h2>
<p>New module <code>src/preprocess.py</code> exposes...</p>
```

## State write pattern

Always **GET → mutate → POST with If-Match**. Never POST without reading first.

```bash
PROJECT="$(basename "$(git rev-parse --show-toplevel)")"
SLUG="<slug>"
ENVELOPE=$(curl -s "http://127.0.0.1:8765/state/$PROJECT/$SLUG")
CUR_VER=$(echo "$ENVELOPE" | jq -r '.data._version // 0')
CURRENT=$(echo "$ENVELOPE" | jq '.data // {} | del(._version)')

# mutate CURRENT with jq, e.g.:
NEW_DATA=$(echo "$CURRENT" | jq '.decisions["my-key"] = {"choice":"a","rationale":"r","when":"2026-06-01","by":"smc"}')

curl -s -X POST \
  -H 'Content-Type: application/json' \
  -H "If-Match: \"$CUR_VER\"" \
  -d "$NEW_DATA" \
  "http://127.0.0.1:8765/state/$PROJECT/$SLUG"
```

Rules:
- Always strip `_version` from the body (`del(._version)`).
- Always merge into the full existing `data` object — never POST a partial payload.
- A missing If-Match header causes HTTP 412. A partial POST destroys unrelated state.
- Server increments `_version`; never set it client-side.
- State files on disk: `~/docs-server/state/<project>/<slug>.json`.

## Intent: prose edit

**Evergreen vs stage-file decision:**

| Edit intent | Treat as | Output |
|---|---|---|
| Typo, broken link, small clarification, new subsection | **Evergreen** | Edit `docs/<slug>.html` in place |
| Adding a followup, note, or question | **State write only** | Edit state JSON; usually no HTML change |
| Recording a decision with rationale | **Evergreen + state write** | POST to state; usually no HTML change |
| Section fully landed; work is done | **Phase transition** | Write `docs/<slug>-<n>-landed.html`; append summary to evergreen |
| Decision locked irreversibly | **Phase transition** | Write `docs/<slug>-<key>-locked.html` |
| Plan fully implemented | **Phase transition + archive** | See §archive below |

**Steps (evergreen):**
1. Read `docs/<slug>.html` end to end.
2. Make the smallest edit that achieves the goal; add new `<h3>` blocks rather than modifying existing paragraphs.
3. Update `<time id="updated">` to today.
4. Suggest commit: `docs(<slug>): <short summary>`. Do not commit unless asked.

**Steps (phase transition):**
1. Determine suffix: `landed`, `locked`, `final`, or section-specific (e.g. `02-landed`).
2. Create `docs/<slug>-<suffix>.html` — frozen snapshot with same site shell, links back to evergreen.
3. Append a short `<h3>` block in the evergreen pointing at the new file.
4. Update the timestamp on the evergreen.

## Intent: decisions

**Lock a single decision:**
1. Read state JSON using GET → mutate → POST with If-Match (see §state write pattern).
2. Set `data.decisions["<key>"] = {choice, rationale, when, by}`.
3. If the decision is irreversible, follow the phase-transition rule (write a `-locked.html`).

**Interactive walkthrough ("walk me through all open decisions"):**
1. GET state; collect `data.decisions` keys where `choice` is null/absent.
2. For each open decision, present the options from the HTML and ask the user to choose.
3. Accumulate choices; do one merged POST with If-Match at the end.
4. Report how many decisions were locked.

**Dissent / reopen flow:**
1. Write a followup (§05 template, `recommends_skill: "/reckon-edit <slug> --reopen <key>"`).
2. Body: what the locked choice was, what evidence changed, what you propose instead.
3. Never silently re-lock. A human or coordinator reviews and accepts or rejects the followup.
4. On `--reopen`: snapshot the existing decision into `<slug>-<key>-locked.html`, remove the lock from state, add a "reopened" badge to the HTML row, write a `notes[]` entry.

## Intent: followup / notes / research

**Append a followup:**
1. GET state; generate `id: "f-<unix-secs-base36>"`.
2. Append to `data.followups[]` with the minimum body below.
3. POST with If-Match. A followup without `prompt` is a hard failure — use the §05 template.

Minimum followup body:
```json
{
  "id": "f-<base36>",
  "written_by": "<agent | smc>",
  "written_at": "YYYY-MM-DD HH:MM",
  "title": "<imperative short title>",
  "body": "<2–3 sentences of context>",
  "recommends_skill": "/reckon-ship <slug> | /reckon-edit <slug> | null",
  "tier": "haiku | sonnet | opus",
  "est_turns": "~1h | ~1d | ~1 sprint",
  "prompt": "<§05 template body — mandatory>"
}
```

**Resolve a followup by id:**
1. GET state; find entry in `data.followups[]` by `id`.
2. Merge in `resolved_at`, `resolved_by`, `outcome`.
3. POST with If-Match.

Notes and research items use the same append pattern; `prompt` is optional for those.

## Intent: sprint management

Operates only on `docs/state/<project>/index.json#sprints[]`.  
Does NOT dispatch workers (that is `reckon-ship`).  
Does NOT read plan HTML content (that is `reckon-ship` / `reckon-status`).

**Propose a sprint:**
1. Load `index.json.plans[]`; filter `status in {active, pending}`.
2. Score: `roi (high=3, mid=2, low=1) × effort_inverse (S=4, M=3, L=2, XL=1) × milestone_priority`.
3. Apply dependency-aware ordering (blocked items trail their blockers).
4. Partition into N sprints (default 4); each item as an object:
   ```json
   {"slug": "my-plan", "why_now": "Highest ROI; gates M1", "tier": "sonnet", "done_when": "tests green"}
   ```
   Use `why_now` (not `justification`). Use `tier` (not `model_tier`).
5. Print proposal as a table; ask user to confirm before POSTing.
6. POST updated `index.json` with If-Match.

**Start a sprint:** Promote `status: "planned"` → `"active"`. Warn if another sprint is already active.

**Close a sprint:** Set `status: "done"`. List any unshipped items; ask how to handle (roll, abandon, or keep open).

**Move item:** Remove from source `items[]`, append to target `items[]` (convert legacy string entries to object form). POST with If-Match.

**Rebalance:** Re-score + diff against current sprints[]; surface proposed moves; ask to confirm before applying.

State path: `docs/state/<project>/index.json` (not `docs/plans-html/state/`).

## Intent: archive

1. Write `docs/<slug>-final.html` — frozen snapshot with final status grid, all locked decisions, implementation commit SHAs, and a link to the evergreen.
2. Move `docs/<slug>.html` to `docs/archive/<slug>.html` (`.html`, not `.md`).
3. Update `docs/index.html` plans table: move the row to "Completed" or drop it.
4. Remove from active `<nav>` or add an "(archived)" marker.
5. Leave state files in place — they are history.
6. POST `data.status = "archived"` and `data.last_modified` to the slug's state endpoint with If-Match.

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
  docs/state/<project>/<plan>.json

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
- `~/Code/reckon/` — docs-server source; CSS lives in `docs/_shared/`.
- `~/docs-server/mounts.json` — mount registry (no restart needed).
