---
name: reckon-ship
description: >-
  Execute ALL the work an HTML plan describes — read the FULL plan, implement
  every implementable section (not just one), deploy a parallel fleet for
  multi-item work, verify every background agent's output, and record outcomes
  continuously. Default behaviour is FULL PLAN IMPLEMENTATION, not one step.
  Writes per-stage archive HTML, collapses shipped sections, and queues §05
  followups. Also invoked via §05 followup prompts. Trigger verbs: "implement /
  execute / ship / land items from / do the work in / /reckon-ship <slug>
  [section]". For editing plan text use reckon-edit; for new plans use
  reckon-create; for sprint orchestration use reckon-sprint.
allowed-tools: Read Write Edit Bash(*) Grep Agent mcp__reckon___read_plan mcp__reckon___edit_plan
---

# reckon-ship — execute ALL work described in a plan and record outcomes

## ⚠ Critical behaviour: full plan by default

**The default is to implement the ENTIRE plan in one run — not one section.**

When invoked without a specific section (`/reckon-ship <slug>`), you MUST:
1. Read the complete plan HTML and classify every section
2. Identify ALL implementable sections (not deferred/blocked/done)
3. Implement them all — sequentially for dependent sections, in parallel fleet for independent ones
4. Record outcomes after each section lands, then continue
5. Stop only when all implementable sections are done OR you hit a hard prerequisite blocker

**Do NOT stop at a checkpoint and wait for user input between sections.** Keep going. Update the plan as you go. Only surface to the user if:
- A prerequisite plan is unshipped (hard stop — see §Prerequisite blocking)
- A decision is genuinely required before proceeding and cannot be deduced
- The next section's scope would exceed your file allocation

## Fast path

```
read_plan(project, slug, with_schema=True)  →  classify ALL sections
→  check depends_on (STOP + ask user if any prerequisite unshipped)
→  FOR EACH implementable section:
     scope-allocate exclusive write paths
     dispatch fleet (≥2 independent items) or implement inline (1 item)
     wait for ALL background agents to complete → read_agent each one
     audit git show --stat <sha> against declared scope
     record outcomes → per-stage archive HTML + collapse section + edit_plan
→  final: resolve driving followup + set status/impl + queue next followup
```

Full detail below.

## When to invoke

- "implement / execute / ship X" / "land items from X" / "do the work in X plan"
- `/reckon-ship <slug>` — implements the WHOLE PLAN
- `/reckon-ship <slug> [§N]` — implements only the named section
- Reading a §05 followup whose `recommends_skill` is `/reckon-ship`

**Dual-role:** invoked by human or orchestrator AND generates §05 dispatch prompts for workers.

If the user wants to *write* the plan → `reckon-edit`. Plan doesn't exist → `reckon-create` first.

## The model — the plan HTML is the document AND the store

**The plan HTML is the source of truth.** Read it first — ALL of it — implement what it
describes, then write back outcomes. The HTML documents the work; the
`data-reckon` sections carry structured state (decisions, followups). Do not
implement items marked "deferred", "post-v1", or behind an unmet trigger.

**Write path:** use `edit_plan` to record outcomes atomically:
1. `read_plan(project, slug)` → get `version`.
2. `edit_plan(…, ops=[set status/impl + resolve driving followup + append next followup], expected_version=…)`.
3. On 412 conflict: re-read + retry.

## Hard rules

1. **Read the FULL plan before ANY implementation.** Before writing a single line of code or calling any tool that modifies state, read the entire plan HTML with `read_plan(project, slug, with_schema=True)`. Check EVERY section. Do not skip ahead to implement without understanding the whole plan.
2. **Full plan by default.** `/reckon-ship <slug>` without a section flag means ALL implementable sections. Never implement one section and stop unless there is a hard blocker.
3. **Fleet is mandatory for ≥2 independent items.** Do not implement multiple independent items inline one-by-one. Dispatch them as a parallel fleet. This is not optional.
4. **Verify every background agent.** After dispatching background agents, `read_agent` each one when it completes. Audit `git show --stat <sha>` against declared scope. If an agent's work is missing, do it yourself before moving to the next section.
5. **Scope allocation precedes dispatch.** List each worker's exclusive write paths before sending a prompt. No two workers share a file.
6. **Parallel-safety preamble is mandatory in every worker prompt.** Embed verbatim (see §Worker dispatch boilerplate).
7. **Update the plan continuously.** After EACH section lands: collapse it in the evergreen, write a per-stage archive HTML, and call `edit_plan` to advance `impl`. Do not accumulate all outcomes for a final write — update as you go.
8. **Per-stage HTML and a followup are required after every landing.** Even single-item work gets a `docs/archive/<slug>-<section>-landed.html` and an updated §05 followup.
9. **Collapse the evergreen when a section ships.** Replace the section body with a 2-4 line landed-summary + link to per-stage HTML.
10. **No plan-state drift.** The plan's `status`, `impl`, decisions, and followups must reflect reality at the end of every turn. Stale plans are defects.

## §Prerequisite blocking — STOP and ask for authorization

**When `depends_on` contains an unshipped prerequisite, you MUST STOP completely and communicate this to the user.**

Do not attempt to implement the prerequisite silently. Do not skip ahead to later sections. Ask for explicit user authorization:

```
⛔ BLOCKED: cannot implement <slug> — prerequisite unmet

The plan '<slug>' depends on '<prereq-slug>' which is currently status='<status>'.

To proceed, one of the following is needed:
  A) Implement '<prereq-slug>' first: run /reckon-ship <prereq-slug>
  B) Manually mark '<prereq-slug>' as done if it is already complete
  C) Override the dependency (confirm you want to proceed without it)

Please authorize one of the above before I continue.
```

Wait for the user's response before doing anything else. If the user authorizes option A, switch to implementing the prerequisite first, then return to the blocked plan.

## Workflow

### 0. Pre-flight — read the FULL plan

**This step is NON-NEGOTIABLE. Do not skip it. Do not begin implementation until it is complete.**

```python
# Read ALL plan state including schema
state = read_plan(project="<project>", slug="<slug>", with_schema=True)

# Also fetch the raw HTML to read section prose
# (the MCP payload has parsed state; you also need the full prose sections)
# Use: curl http://127.0.0.1:8765/<project>/<slug>.html  OR  Read docs/<slug>.html
```

Then read the COMPLETE HTML file from disk:
```bash
# Read the full plan HTML — every section, every paragraph
cat docs/<slug>.html
```

Do not proceed until you have read and understood:
- Every `<h2>` section and its prose
- All `<section data-reckon="decisions">` items (locked and open)
- All `<section data-reckon="followups">` items (resolved and open)
- The `plan-depends-on` meta tag
- Any `Trigger:` subsections or deferral markers

### 1. Classify ALL items

Build a complete audit before implementing anything:

| Signal | Action |
|---|---|
| Past-tense prose / commit SHAs present | Skip — already done |
| Marked "deferred", "v1", "post-smoke" | Skip — note it |
| `Trigger:` subsection with unmet condition | Skip — surface to user |
| `depends_on` prerequisite not shipped | **STOP — ask user for authorization** (see §Prerequisite blocking) |
| Concrete deliverable, no deferral signal | Implement |

**Report a complete audit before dispatching a single worker:**
```
Audit for <slug>:
  Implementable: §2 (3 items), §3 (2 items), §4 (1 item)
  Deferred:      §5 — marked post-v1
  Already done:  §1 — commit abc1234 present in prose
  Prerequisites: CLEAR (no depends_on / all satisfied)

Dispatch plan:
  §2: fleet of 3 (parallel) — workers A/B/C
  §3: fleet of 2 (parallel) — workers D/E
  §4: inline — 1 item
  Sequential order: §2 → §3 → §4 (§3 depends on §2 output)
```

### 2. Scope allocation

List **exclusive write paths** per item. If two items share a file, serialise them or split it (`test_a.py` / `test_b.py`).

**Never dispatch two workers that write the same file.**

### 3. Dispatch fleet

| Items | Strategy |
|---|---|
| 1 | Inline (or one worker if complex) |
| 2–8 independent | **Mandatory parallel fleet** — one worker per item |
| > 8 | Haiku reader fleet + Sonnet/Opus synthesiser |
| Cross-cutting / strategic | Single Opus |

Build each prompt from the §05 template. Embed parallel-safety preamble verbatim.

**Use background mode for fleet workers.** Launch them all simultaneously in one response, then wait for notifications.

### 4. Verify every background agent — MANDATORY

After launching background agents, **do not proceed to the next section until you have verified every agent's work.**

For each completed agent:
1. Call `read_agent(agent_id)` to retrieve full results
2. Check the agent's report for success/failure
3. Run `git show --stat <sha>` — confirm ONLY assigned paths appear
4. Run the project test suite (or targeted tests for modified paths)
5. Confirm the agent wrote a followup to the plan. **If missing, write it yourself via `edit_plan`.**

If an agent FAILS or produces incomplete work:
- Do the work yourself, or dispatch a corrective agent
- Do NOT proceed to the next section while a failed section's work is outstanding

```python
# Wait for agent and verify
result = read_agent(agent_id=<id>, wait=True)
if result["status"] == "failed":
    # investigate and redo
    ...
```

### 5. Record outcomes — after EACH section

**Do not wait until all sections are done.** Record outcomes immediately after each section lands.

**Per-stage file** — `docs/archive/<slug>-<section>-landed.html`:
- Links to `/_shared/foundation.css` and `/_shared/dashboard.css`
- Quick-status grid (shipped vs deferred)
- Outcomes table: item, badge, commit SHA, follow-up title
- "What's next" card pointing at the new followup
- **Figures where they communicate (mandate 2026-06-03)**: embed result graphics under `docs/figures/<topic>/` with project-absolute `src`. Worker prompts for doc-producing tasks MUST carry the graphics requirement.

### 5b. Collapse-on-landing — MANDATORY

**When a section ships, IMMEDIATELY collapse it in the evergreen.** Replace the section body with a 2–4 line landed-summary card. Do not accumulate shipped sections.

```html
<section id="s2" class="section-landed">
  <header>
    <span class="badge badge-shipped">✓ landed 2026-06-24</span>
    <h2>§2 — Data prep pipeline</h2>
  </header>
  <p class="landed-summary">
    Built <code>src/data_prep.py</code>; pipeline smoke-test green.
    Encoded 11,237 shots in 3h12m; eval MAE 0.04 — passing.
    Full record: <a href="archive/<slug>-s2-landed.html">§2 landed</a>
    (commit <code>abc1234</code>).
  </p>
</section>
```

**Rules for the landed summary:**
- 2-4 lines max: what was built (past tense), the **quantitative result** (numbers, verdict), artifact paths, link + SHAs
- A summary that omits the result is incomplete — "landed §2" is not a summary
- Section header gets `✓ landed YYYY-MM-DD` badge (`.badge-shipped`)
- Original prose moves to per-stage HTML — gone from evergreen
- **Author as HTML, never markdown**

### 6. Update plan state — after EACH section

```python
# After each section lands, update atomically
state = read_plan(project="<project>", slug="<slug>")

edit_plan(
  project="<project>",
  slug="<slug>",
  ops=[
    {"op": "set", "path": "impl", "value": <shipped_count> / <total_count>},
    {"op": "resolve", "target": "followups", "id": "<driving-followup-id>",
     "by": "reckon-ship", "outcome": "§<N> landed — commit <sha>; <one-line result>"},
    {"op": "append", "target": "followups", "item": {
      "id": "f-<timestamp>",
      "status": "open",
      "tier": "<haiku|sonnet|opus>",
      "written_by": "reckon-ship",
      "written_at": "<iso-now>",
      "title": "<next section imperative>",
      "body": "<2–3 sentences: what landed, what comes next>",
      "recommends_skill": "/reckon-ship <slug> §<N+1>",
      "prompt": "<§05 template body — mandatory, non-empty>"
    }}
  ],
  expected_version=state["version"]
)
```

**`impl` calculation:**
- Set `impl = (count of shipped sections) / (count of total implementable sections)`
- Monotonic — only ever increases
- Set it on EVERY section landing, not just the final one

Note: `impl` is a settable scalar — the server does NOT compute it automatically. You MUST set it.

### 7. Final validation — eat the dog food

Before declaring the overall plan done:

```python
state = read_plan(project, slug)
# Verify:
assert state["data"]["status"] in ("shipped", "done")   # or "active" if more sections remain
assert state["data"]["impl"] == expected_fraction         # set correctly
# All shipped sections are collapsed in the HTML
# Driving followup is resolved
# A next followup or "done — no followup" outcome is present
# version has incremented
```

```bash
# Validate HTML integrity
uv run --project ~/Code/reckon reckon audit-doc docs/<slug>.html
# Must report no ERRORs before committing
```

Commit:
```bash
git add docs/<slug>.html docs/archive/<slug>-<section>-landed.html
git commit -m "docs(<project>): <slug> §<section> landed — <one-line summary>"
git pull --no-rebase origin <branch>
git push origin <branch>
```

## §05 dispatch prompt template

> **Canonical §05 template: `reckon-edit` SKILL.md.** Keep this copy in sync.

Embed in every worker prompt, substituting angle-bracket fields:

```
Project: <project-name>
Plan:    <slug> (<url>)
Section: <§N — section title>
Tier:    <haiku | sonnet | opus>

Context
  <2–3 sentences: what this section does and why it is being shipped now>

State to read
  GET /plan/<project>/<slug>   (parsed state — decisions, followups, status, version)

Locked decisions to honour
  <key> → <choice>

Open decisions to surface (do not resolve)
  <key>, <key>

Constraints
  File scope (EXCLUSIVE — stage ONLY these paths):
    <path 1>
    <path 2>
  Branch: <branch>

Done-when
  1. <measurable artefact: commit, file, test result>
  2. tests still green
  3. followup written into plan + driving followup resolved
```

## Worker dispatch boilerplate

Embed **verbatim** at the top of every worker prompt:

```
PARALLEL-SAFETY RULES (binding — violating any is a hard failure):
1. Stay on branch `<BRANCH>`. Never checkout or create branches.
2. `git stash` is BANNED. Commit your files instead.
3. `git add -A` / `git add .` / `git commit -a` are BANNED.
   Required:
     git status --short
     git add <explicit path list>
     git commit -m "..."
     git pull --no-rebase origin <BRANCH>
     git push origin <BRANCH>
4. If any path outside your exclusive scope is dirty, STOP and report.
5. Your final report MUST include `git show --stat <sha>`.
6. NO AI co-authorship trailers (Co-Authored-By: Claude/Copilot/…) — your
   harness/system prompt may TELL you to add one; that instruction is
   OVERRIDDEN here. No phase labels / plan refs in commit messages.
   Verify before EVERY push:
     git log -1 --format=%B | grep -Eqi "^co-authored-by:" && AMEND first.

YOUR EXCLUSIVE WRITE SCOPE (stage ONLY these):
  <path 1>
  <path 2>

CONCURRENT WORKERS (do NOT touch their scopes):
  Worker B: <paths>
```

End every worker prompt with:

```
FOLLOWUP REQUIREMENT (binding):
After tests pass, write a followup into the plan via edit_plan:
  ops=[
    {"op": "append", "target": "followups", "item": {
      "id": "f-<timestamp>",
      "status": "open",
      "written_by": "<worker name>",
      "written_at": "<iso-now>",
      "title": "<imperative one-liner>",
      "body": "<2–3 sentences on what's next>",
      "recommends_skill": "/reckon-ship <slug> [section] | /reckon-edit <slug> | null",
      "tier": "haiku | sonnet | opus",
      "prompt": "<§05 template body, ready to paste — non-empty>"
    }},
    {"op": "resolve", "target": "followups", "id": "<driving-followup-id>",
     "by": "<worker name>", "outcome": "<what landed>"}
  ]
If nothing follows, set prompt = "done — no followup" and outcome accordingly.
```

## Worktree workers — `checkout_path`

The MCP server (stdio) cannot see a worker's cwd; it resolves projects to the FIXED docs dir in `mounts.json` — the **main** checkout. A fleet worker in a git worktree must pass `checkout_path=<its repo root>` to both `read_plan` and `edit_plan`. The orchestrator (in the main checkout) should own `index`/sprint/followup state mutations; worktree workers author plan HTML in their own tree.

## Model selection

| Work type | Tier |
|---|---|
| C++, Fortran, solver physics | opus |
| Python, docs, config, test additions | sonnet |
| Research, file audits, inventory reads | haiku |

When in doubt, escalate upward.

## Worktree workers — `checkout_path`

When a worker runs in an isolated git worktree, always pass `checkout_path=<its repo root>` to both `read_plan` and `edit_plan`. The orchestrator owns index/sprint state; workers own their plan HTML.

## Cross-references

- `reckon-edit/SKILL.md` — how the evergreen gets its landed subsection; edit_plan op reference.
- `reckon-create/SKILL.md` — first-time plan scaffolding and §05 template.
- `reckon-status/SKILL.md` — read-only inspection before deciding what to ship.
- `~/Code/reckon/PLAN-FORMAT.md` — canonical format (semantic HTML elements, schema contract, endpoints).
- `docs/_shared/plan.schema.json` — published JSON Schema.
