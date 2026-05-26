---
name: reckon-status
description: >-
  Read-only inspection of plans and sprint state — phase, status, ROI, effort,
  milestone, implementation fraction, open decisions, blockers, active sprint
  progress, and quality audit (empty followup prompts, stale timestamps, missing
  NEXT cards). Any HTML file in docs/ is a plan; island carries full state.
  Never modifies files. Trigger verbs: "what plans are open / where are we on X /
  show plan status / what sprint are we on / summarise plans / review the plan /
  audit plan health / is the plan stale / /reckon-status [slug]".
allowed-tools: Read Bash(*) Grep
---

# reckon-status — read-only plan inspection and quality audit

## When to invoke

**Intent: status** — user asks any of:
- "what plans are open?" / "show plan status" / "where are we on X?"
- "what sprint are we on?" / "summarise plans" / `/reckon-status`

**Intent: review** — user asks any of:
- "review the plan" / "audit plan health" / "is the plan stale?"
- "any quality issues with the plans?" / `/reckon-status --review`

**Never writes.** This skill does not modify any file and does not POST
to the docs-server. If you need to fix what you find, use `reckon-edit`.

## Hard rules

1. **Pure read.** Never write a file. Never POST to the docs-server.
2. **Literal.** Report what the island state says; do not invent status.
3. **Synthetic examples only.** Use `plan-alpha`, `plan-beta`,
   `my-project` as examples — never real project names.
4. **One suggestion, not an action.** Offer a single next-step hint
   at the end; do not execute it.

---

## Intent: status

### Step 1 — discover plans

```bash
REPO_ROOT="$(git rev-parse --show-toplevel)"
PROJECT="$(basename "$REPO_ROOT")"
```

**Preferred:** query the server's discovery endpoint:
```bash
curl -s "http://127.0.0.1:8765/_discover/$PROJECT"
```
This returns all plans with their full island state merged in — one source.

**Fallback (server not running):** glob `docs/*.html` (skip `_shared/`, `ui/`,
`state/`, `assets/`, `archive/` subdirectories and known infrastructure pages:
`index.html`, `sprints.html`, `milestones.html`, `decisions.html`,
`inventory.html`, `blockers.html`, `questions.html`, `home.html`,
`project.html`). Read each file's `<script type="application/json"
id="reckon-state">` island for state. A file with no island surfaces as
`status=draft` with `<title>` as the title — existence is sufficient.

**Any HTML file in `docs/` is a plan.** No `plan-status` meta opt-in required.

### Step 2 — read project config

```bash
INDEX_JSON="$REPO_ROOT/docs/state/$PROJECT/index.json"
```

If present, read it for sprint and milestone definitions. It holds
**project-level config only** (sprints, milestones, `active_sprint_id`,
timeline) — not per-plan state. Per-plan state comes from each plan's island.

### Step 3 — surface open decisions

For each plan, read `decisions` from the island (a **map** keyed by decision
key). List entries where `choice` is `""` or null.

Format: `- <slug>: [<key>] "<title>" options: <choices-joined>`

### Step 4 — surface unresolved followups

For each plan, list followup entries where `status != "resolved"`.
Show: `plan-slug / <id>: "<title>" (written <age>)`.

### Step 5 — suggest one next action

Scan the report and offer the single most actionable next step.
Do not execute it.

---

## Intent: review

Run all checks below and emit a prioritised punch-list. Fixes go
through `reckon-edit`, never here.

| Check | Condition |
|-------|-----------|
| **Empty followup prompt** | followup entry exists but `prompt` is null/empty |
| **Missing NEXT card** | `status=active` plan has no open followup in island |
| **Open decision** | `decisions` map has entry where `choice` is `""` or null |
| **Stale plan** | `status=active` and `modified` > 30 days ago |
| **Tier mismatch** | sprint item `tier` differs from plan-level `tier` in island |

Output format for review:

```
## Plan Quality Audit — <project> (<date>)

### High priority (N)
- plan-alpha: followup f-001 has empty prompt — add text or remove entry
- plan-beta: active but no open followup in island

### Medium priority (N)
- plan-alpha: decision [storage] is open — choice not yet set

### Low priority (N)
- plan-alpha: modified 45d ago, status still active — confirm or ship
- plan-beta: item tier=haiku but plan tier=sonnet
```

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
- plan-beta: [storage] not yet defined

### Unresolved followups (N total)
- plan-alpha / f-001: "Implement alias layer" (written 3d ago)

### Suggested next action
> Run `/reckon-ship plan-alpha §3` — 3 items implementable, S2 in progress
```

For ≤ 2 plans, skip the table; give one paragraph per plan inline.

---

## Cross-references

- `~/.claude/skills/reckon-edit/SKILL.md` — all mutations (prose, decisions, sprints, archive).
- `~/.claude/skills/reckon-create/SKILL.md` — create new plans.
- `~/.claude/skills/reckon-ship/SKILL.md` — execute plan work via fleet dispatch.
- `~/Code/reckon/PLAN-FORMAT.md` — canonical format (island schema, discovery, endpoints).
- `~/Code/reckon/` — reckon project (server). Start: `uv run --project ~/Code/reckon reckon serve`
