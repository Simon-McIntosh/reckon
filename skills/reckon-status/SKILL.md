---
name: reckon-status
description: >-
  Read-only inspection of plans and sprint state — phase, status, ROI, effort,
  milestone, implementation fraction, open decisions, blockers, active sprint
  progress, and quality audit (empty followup prompts, stale timestamps, missing
  NEXT cards). Handles both per-doc and central-index layouts. Never modifies
  files. Trigger verbs: "what plans are open / where are we on X / show plan
  status / what sprint are we on / summarise plans / review the plan / audit
  plan health / is the plan stale / /reckon-status [slug]".
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
2. **Literal.** Report what the state files say; do not invent status.
3. **Synthetic examples only.** Use `plan-alpha`, `plan-beta`,
   `my-project` as examples — never real project names.
4. **One suggestion, not an action.** Offer a single next-step hint
   at the end; do not execute it.

---

## Intent: status

### Step 1 — detect layout

```bash
REPO_ROOT="$(git rev-parse --show-toplevel)"
PROJECT="$(basename "$REPO_ROOT")"
INDEX_JSON="$REPO_ROOT/docs/state/$PROJECT/index.json"
STATE_DIR="$HOME/docs-server/state/$PROJECT"
```

- If `$INDEX_JSON` exists → **central-index layout**.
- Otherwise → **per-doc layout** (glob `docs/*.html`, read
  `$STATE_DIR/<slug>.json` per plan).

### Step 2 (central-index) — read index.json

Read `$INDEX_JSON`. Surface per plan:
`slug`, `title`, `status`, `sprint`, `roi`, `effort`, `tier`,
`impl_fraction` (compute as `shipped_count / total_count` if absent).

Per sprint, compute `done / total` and identify the active sprint
(field `status: "active"`, else lowest-id sprint with progress < 1).

### Step 2 (per-doc) — walk docs/*.html

For each `<slug>.html` (skip `index.html`):
- Title: `<title>` or first `<h1>`.
- Status: `data-status` on `#plan-meta`, or badge class, or infer from
  per-stage files (`-landed.html` → landed).
- Last-modified: `<time id="updated">` or
  `git log -1 --format=%cI -- docs/<slug>.html`.
- Read `$STATE_DIR/<slug>.json` for decisions and followups.

### Step 3 — surface open decisions

For each plan, list decisions where `choice` is null or empty.
Support both state-file shapes (see §Decision schema detection below).

### Step 4 — surface unresolved followups

For each plan, list followup entries where `status != "done"`.
Show: `plan-slug / <id>: "<prompt>" (written <age>)`.

### Step 5 — suggest one next action

Scan the report and offer the single most actionable next step.
Do not execute it.

---

## Intent: review

Run all checks below and emit a prioritised punch-list. Fixes go
through `reckon-edit`, never here.

| Check | Condition |
|-------|-----------|
| **Empty followup** | followup entry exists but `prompt` is null/empty |
| **Missing NEXT card** | `status=active` plan has no `NEXT` section in HTML |
| **Orphan decision** | `<table>` decision row in HTML has no matching `data.decisions` entry |
| **Stale plan** | `status=active` and `last_modified` > 30 days ago |
| **Tier mismatch** | sprint item `tier` differs from plan-level `tier` |

Output format for review:

```
## Plan Quality Audit — <project> (<date>)

### 🔴 High priority (N)
- plan-alpha: followup f-001 has empty prompt — add text or remove entry
- plan-beta: active but no NEXT card in HTML

### 🟡 Medium priority (N)
- plan-alpha: decision [storage] in HTML table but missing from data.decisions

### 🟢 Low priority (N)
- plan-alpha: last_modified 45d ago, status still active — confirm or ship
- plan-beta: item tier=haiku but plan tier=sonnet
```

---

## Decision schema detection

State files use one of two shapes. Detect and handle both:

**Shape A — nested map under `data.decisions`:**
```json
{ "data": { "decisions": { "transport": { "choice": "stdio", "options": ["stdio","http"] } } } }
```
Open = entries where `choice` is null or `""`.

**Shape B — legacy top-level keys with `choice` property:**
```json
{ "transport": { "choice": null, "options": ["stdio","http"] } }
```
Open = keys (excluding known metadata keys) where `choice` is falsy.

In either shape, list open decisions as:
`- <slug>: [<key>] options: <options-joined>` or `not yet defined` if
options is empty.

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
- plan-alpha: [transport] options: stdio, http
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
- `~/Code/reckon/` — reckon project (server, canonical CSS). Start: `uv run --project ~/Code/reckon reckon serve`
