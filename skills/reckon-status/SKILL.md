---
name: reckon-status
description: >-
  Read-only inspection of plans and sprint state — phase, status, ROI, effort,
  milestone, implementation fraction, open decisions, blockers, active sprint
  progress, and quality audit (empty followup prompts, stale timestamps, missing
  NEXT cards). Typed HTML resources carry stable identity and semantic state.
  Never modifies files. Trigger verbs: "what plans are open / where are we
  on X / show plan status / what sprint are we on / summarise plans / review the
  plan / audit plan health / is the plan stale / /reckon-status [slug]".
allowed-tools: Read Bash(*) Grep mcp__reckon___read_plan mcp__reckon___audit mcp__reckon___roadmap
---

# reckon-status — read-only plan inspection and quality audit

## Fast path
- What's open / where are we → `read_plan(project, view="summary")`.
- Status of one plan → `read_plan(resource={project,type:"plan",id:slug})`.
- What to ship next / true blockers / critical path → use `reckon-roadmap` and the `roadmap` tool.
- Audit health → call `audit(project, view="summary")`, then request detail if findings exist.

Full detail below.

## When to invoke

**Intent: status** — "what plans are open?" / "show plan status" / "where are we on X?" / `/reckon-status`

**Intent: review** — "review the plan" / "audit plan health" / "is the plan stale?" / `/reckon-status --review`

**Never writes.** This skill does not modify any file and does not call `edit_plan` or POST
to the docs-server. Fixes go through `reckon-edit`.

## Hard rules

1. **Pure read.** Never write a file. Never call `edit_plan`. Never POST to the docs-server.
2. **Literal.** Report what the plan's semantic HTML says; do not invent status.
3. **Report what is there.** Use the real project and plan names from the repo — the purpose of this skill is real-status reporting. Use synthetic names (`plan-alpha`, `my-project`) only in examples inside this SKILL.md document itself.
4. **One suggestion, not an action.** Offer a single next-step hint; do not execute.

---

## The read tools

**Discovery (whole project):**
```python
# Returns a compact, paginated inventory plus project rollups
state = read_plan(project="my-project", view="summary")
```

**Single plan:**
```python
state = read_plan(
    resource={"project": "my-project", "type": "plan", "id": "plan-alpha"}
)
# state["version"], state["state"]["status"], state["open_decisions"], …
```

**Single plan + schema:**
```python
state = read_plan(
    resource={"project": "my-project", "type": "plan", "id": "plan-alpha"},
    view="schema",
)
# Includes the progressive response schema and selected storage schema
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

Use `read_plan(project, view="summary")` for compact typed discovery, or
`curl -s "http://127.0.0.1:8765/_discover/$PROJECT"`.

### Discovery rules

**Discovery uses declared typed roots.** Plans, research, evidence, and sprint
definitions live under `docs/plans/`, `docs/research/`, `docs/evidence/`, and
`docs/sprints/`. Bounded flat compatibility resources remain readable.

**Excluded dirs:** `_shared`, `_ui`, `ui`, `state`, `assets`, `images`, `figures`.

**Excluded files:** `index.html`, `sprint.html`, `sprints.html`, `milestones.html`, `decisions.html`, `inventory.html`, `blockers.html`, `implementation.html`, `questions.html`, `home.html`, `project.html`.

Per-stage history lives under the owning type's `archive/` directory.

**`reckon-type=research`** (or `doc`, normalised to `research`) plans show with a "research" banner in the SPA; they appear in the inventory with `type="research"`. They have no decision/followup workflow.

**Visibility flags:**
- `plan-archived=1` hides a plan from the default inventory view.
- `plan-read=1` marks a research/doc as reviewed.

### Step 2 — read project config

Use `read_plan(resource={project,type:"project",id:"project"})` for the compact
project state. Request `view="detail"` only when the sprint, milestone, blocker,
or timeline cards are needed. In distributed mode the retained on-disk
`index.json` is a frozen migration source, not current state. Plan lifecycle
state still comes from plan HTML.

### Step 3 — surface open decisions

For each plan, read `decisions` from parsed state (map keyed by `data-key` on `.r-dec` elements). Open decision: `data-choice=""` or absent.

Format: `- <slug>: [<key>] "<title>" options: <choices>`

### Step 4 — surface unresolved followups

For each plan, list followups where `data-status != "resolved"` and `data-resolved-at` is absent.

Note: `status=resolved` is **derived** from `data-resolved-at` being non-empty. A followup
with `data-resolved-at` set is resolved regardless of `data-status`.

Format: `plan-slug / <id>: "<title>" (written <age>)`

### Step 5 — read executable order

Invoke `roadmap(project)` or hand off to `reckon-roadmap`. Do not reproduce its
graph traversal in prose or shell code. Report:

- lifecycle completion and stored implementation completion;
- `ready_now` in `immediate_roadmap` order;
- exact causes from `blocked` (`depends_on`, explicit blocker, persisted state,
  or cycle), with non-runnable drafts reported from `deferred`;
- weighted `critical_path` and bounded `open_paths`;
- error/warn `wiring_findings`, separated from true external blockers.

The shared analyzer detects invalid/dangling/non-executable hard dependencies,
inactive prerequisites, contradictory relations, cycles, sprint-order
inversions, membership disagreement, and orphaned blocked state. Research and
evidence belong in `informs`; they are not executable prerequisites.

### Step 6 — suggest one next action

Scan the report; offer the first entry from `immediate_roadmap`. Do not execute it.

Only a plan returned in `ready_now` may be the suggested next action. If it is
empty, report the smallest true blocker or highest-severity wiring repair.

---

## Intent: review

**Call `audit(project, view="summary")` first — it is the source of truth.**
If counts are non-zero, request `view="detail"` and page with its cursor. The
detail response returns findings (each with `category`, `code`, `severity`,
`slug`, `path`) plus violations and finding counts.
Render those findings into the punch-list below grouped by severity. Do NOT re-derive the
codes it already emits by hand — they would drift from the code.

`audit` already covers, among others:
- **schema conformance** — `violations[]` (empty followup prompt, missing required fields,
  off-enum status, parse errors)
- **lifecycle** — `MISSING_IMPL`, `STALE`, `STALE_RCA`
- **references** — stale/broken internal links (`audit_links`)
- **sprint** — `multiple-active-sprints`, `active-sprint-missing`, `active-sprint-mismatch`,
  `sprint-item-missing-plan`, `sprint-item-duplicate`, `plan-sprint-missing`,
  `plan-sprint-missing-item`, `plan-sprint-mismatch`

**Then add only the heuristic checks `audit` and `roadmap` do NOT do** (these
need prose-reading):

| Extra check | Condition |
|-------|-----------|
| **Missing NEXT card** | `plan-status=active` plan has no open `<article class="r-fu" data-status="open">` |
| **Sparse relationship metadata** | plan prose clearly references another live plan/research doc but `plan-depends-on` / `plan-blocks` / `plan-informs` is empty |
| **Stale markdown link** | internal `<a href=\"...md\">` still points at a markdown source after migration (beyond what `audit_links` flags) |

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
- plan-beta: legacy tier remains in sprint or plan state — migrate explicitly
```

When reviewing a project after a migration wave, add three short passes:
1. **Inventory** — live plan vs research vs archive counts
2. **Relationship audit** — use `roadmap.wiring_findings`; do not reimplement it
3. **Sprint fit** — only actionable live plans belong in sprint items

---

## Output format (status report)

```
## Plan Status — <project> (<date>)

### Active plans (N)
| Slug       | Status | Sprint | ROI  | Effort | Capability | Progress |
|------------|--------|--------|------|--------|------------|----------|
| plan-alpha | active | S2     | high | M      | general    | 40%     |
| plan-beta  | active | S1     | med  | S      | routine    | 10%     |

### Open decisions (N total)
- plan-alpha: [transport] "How should transport work?" options: stdio, http

### Unresolved followups (N total)
- plan-alpha / f-001: "Implement alias layer" (written 3d ago)

### Suggested next action
> Run `/reckon-ship plan-alpha §3` — 3 items implementable, S2 in progress
```

For ≤ 2 plans, skip the table; give one paragraph per plan.

---

## Cross-references

- `reckon-edit/SKILL.md` — all mutations (prose, decisions, sprints, archive) via `edit_plan`.
- `reckon-create/SKILL.md` — create new plans.
- `reckon-ship/SKILL.md` — execute plan work.
- `reckon-roadmap/SKILL.md` — pending work, true blockers, sprint order, and critical paths.
- `~/Code/reckon/PLAN-FORMAT.md` — canonical format (semantic HTML, schema contract, discovery, endpoints).
- `docs/_shared/plan.schema.json` — published JSON Schema.
