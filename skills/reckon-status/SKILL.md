---
name: reckon-status
description: >-
  Read-only inspection of plans and sprint state — phase, status, ROI, effort,
  milestone, implementation fraction, open decisions, blockers, active sprint
  progress, and quality audit (empty followup prompts, stale timestamps, missing
  NEXT cards). Any HTML file in docs/ is a plan; semantic HTML elements carry full
  state. Never modifies files. Trigger verbs: "what plans are open / where are we
  on X / show plan status / what sprint are we on / summarise plans / review the
  plan / audit plan health / is the plan stale / /reckon-status [slug]".
allowed-tools: Read Bash(*) Grep mcp__reckon___read_plan mcp__reckon___audit
---

# reckon-status — read-only plan inspection and quality audit

## When to invoke

**Intent: status** — "what plans are open?" / "show plan status" / "where are we on X?" / `/reckon-status`

**Intent: review** — "review the plan" / "audit plan health" / "is the plan stale?" / `/reckon-status --review`

**Never writes.** This skill does not modify any file and does not call `edit_plan` or POST
to the docs-server. Fixes go through `reckon-edit`.

## Hard rules

1. **Pure read.** Never write a file. Never call `edit_plan`. Never POST to the docs-server.
2. **Literal.** Report what the plan's semantic HTML says; do not invent status.
3. **Synthetic examples only.** Use `plan-alpha`, `plan-beta`, `my-project` — never real project names.
4. **One suggestion, not an action.** Offer a single next-step hint; do not execute.

---

## The read tools

**Discovery (whole project):**
```python
# Returns inventory + followups/questions/sprints facets for all plans
state = read_plan(project="my-project", slug=None)
```

**Single plan:**
```python
state = read_plan(project="my-project", slug="plan-alpha")
# state["version"], state["data"]["status"], state["data"]["decisions"], …
```

**Single plan + schema:**
```python
state = read_plan(project="my-project", slug="plan-alpha", with_schema=True)
# Includes the JSON Schema and dos/don'ts inline — useful for authoring audit
```

**HTTP fallback (server not running):**
```bash
curl -s "http://127.0.0.1:8765/_discover/my-project"
curl -s "http://127.0.0.1:8765/plan/my-project/plan-alpha"
```

**Offline fallback:** glob `docs/*.html`; skip infrastructure dirs. Read each file's
`<meta name="plan-*">` scalars and `<section data-reckon="…">` elements. A file with
no plan markup surfaces as `status=draft` with `<title>` as its title.

---

## Intent: status

### Step 1 — discover plans

```bash
REPO_ROOT="$(git rev-parse --show-toplevel)"
PROJECT="$(basename "$REPO_ROOT")"
```

Use `read_plan(project, slug=None)` for full parsed state, or
`curl -s "http://127.0.0.1:8765/_discover/$PROJECT"`.

### Discovery rules

**Any HTML file in `docs/` is a plan.** No `plan-status` opt-in required. Existence is sufficient. Sparse plans with no markup show as `status=draft` — they are NOT being filtered.

**Excluded dirs:** `_shared`, `ui`, `state`, `assets`, `images`, `archive`.

**Excluded files:** `index.html`, `sprint.html`, `sprints.html`, `milestones.html`, `decisions.html`, `inventory.html`, `blockers.html`, `implementation.html`, `questions.html`, `home.html`, `project.html`.

Per-stage history (`<plan>-shipped.html`, `*-locked.html`, …) lives under `docs/archive/` so it does not appear in the live inventory.

**`reckon-type=research`** (or `doc`, normalised to `research`) plans show with a "research" banner in the SPA; they appear in the inventory with `type="research"`. They have no decision/followup workflow.

**Visibility flags:**
- `plan-archived=1` hides a plan from the default inventory view.
- `plan-read=1` marks a research/doc as reviewed.

### Step 2 — read project config

If `docs/state/$PROJECT/index.json` exists (or via `read_plan(project, "index")`), read it for sprint and milestone definitions. It holds project-level config only — not per-plan state.

### Step 3 — surface open decisions

For each plan, read `decisions` from parsed state (map keyed by `data-key` on `.r-dec` elements). Open decision: `data-choice=""` or absent.

Format: `- <slug>: [<key>] "<title>" options: <choices>`

### Step 4 — surface unresolved followups

For each plan, list followups where `data-status != "resolved"` and `data-resolved-at` is absent.

Note: `status=resolved` is **derived** from `data-resolved-at` being non-empty. A followup
with `data-resolved-at` set is resolved regardless of `data-status`.

Format: `plan-slug / <id>: "<title>" (written <age>)`

### Step 5 — suggest one next action

Scan the report; offer the single most actionable next step. Do not execute it.

---

## Intent: review

Run all checks; emit a prioritised punch-list.

| Check | Condition |
|-------|-----------|
| **Empty followup prompt** | `<article class="r-fu">` exists but `<pre class="r-fu-prompt">` absent or empty |
| **Missing NEXT card** | `plan-status=active` plan has no open `<article class="r-fu" data-status="open">` |
| **Open decision** | `<div class="r-dec">` with empty or absent `data-choice` |
| **Stale plan** | `plan-status=active` and `plan-modified` > 30 days ago |
| **Tier mismatch** | sprint item `tier` differs from `<meta name="plan-tier">` on the plan page |
| **Non-actionable sprint item** | sprint item slug resolves to a research/doc, archived plan, or done plan |
| **Archived flag but not archived status** | `plan-archived=1` but `plan-status != archived` |
| **Stale markdown link** | internal `<a href=\"...md\">` still points at a markdown source after migration |
| **Sparse relationship metadata** | plan prose clearly references another live plan/research doc but `plan-depends-on` / `plan-blocks` / `plan-informs` is empty |

Output format:

```
## Plan Quality Audit — <project> (<date>)

### High priority (N)
- plan-alpha: followup f-001 has empty prompt — add text or remove entry
- plan-beta: active but no open followup

### Medium priority (N)
- plan-alpha: decision [storage] is open — choice not yet set

### Low priority (N)
- plan-alpha: modified 45d ago, status still active — confirm or ship
- plan-beta: item tier=haiku but plan tier=sonnet
```

When reviewing a project after a migration wave, add three short passes:
1. **Inventory** — live plan vs research vs archive counts
2. **Relationship audit** — explicit `depends_on` / `blocks` / `informs`
3. **Sprint fit** — only actionable live plans belong in sprint items

---

## Output format (status report)

```
## Plan Status — <project> (<date>)

### Active plans (N)
| Slug       | Status | Sprint | ROI  | Effort | Tier   | Progress |
|------------|--------|--------|------|--------|--------|---------|
| plan-alpha | active | S2     | high | M      | sonnet | 40%     |
| plan-beta  | active | S1     | med  | S      | haiku  | 10%     |

### Open decisions (N total)
- plan-alpha: [transport] "How should transport work?" options: stdio, http

### Unresolved followups (N total)
- plan-alpha / f-001: "Implement alias layer" (written 3d ago)

### Suggested next action
> Run `/reckon-implement plan-alpha §3` — 3 items implementable, S2 in progress
```

For ≤ 2 plans, skip the table; give one paragraph per plan.

---

## Cross-references

- `reckon-edit/SKILL.md` — all mutations (prose, decisions, sprints, archive) via `edit_plan`.
- `reckon-create/SKILL.md` — create new plans.
- `reckon-implement/SKILL.md` — execute plan work.
- `~/Code/reckon/PLAN-FORMAT.md` — canonical format (semantic HTML, schema contract, discovery, endpoints).
- `docs/_shared/plan.schema.json` — published JSON Schema.
