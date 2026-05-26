---
name: reckon-ship
description: >-
  Execute the work an HTML plan describes — read the plan, identify implementable
  vs deferred items, dispatch a Sonnet fleet for multi-item sections with
  non-overlapping file scopes, then record outcomes by writing a per-stage HTML
  (<slug>-<section>-landed.html), appending a landed summary to the evergreen,
  and writing a §05 followup. Also invoked via §05 followup prompts queued by
  earlier reckon-ship runs. Trigger verbs: "implement / execute / ship / land
  items from / do the work in / /reckon-ship <slug> [section]". For editing
  plan text use reckon-edit; for new plans use reckon-create; for sprint
  orchestration use reckon-edit (sprint intent).
allowed-tools: Read Write Edit Bash(*) Grep Agent
---

# reckon-ship — execute work described in a plan and record outcomes

## When to invoke

Trigger on any of:

- "implement / execute / ship X" / "land items from X"
- "do the work in X plan" / "/reckon-ship `<slug>` [section]"
- reading a §05 followup whose `recommends_skill` is `/reckon-ship`

This skill is **dual-role**: it is invoked by a human or orchestrator AND it
generates §05 dispatch prompts for worker agents. Both entry points share the
same workflow from Step 2 onward.

If the user wants to *write* the plan, call `reckon-edit`. If the plan does not
yet exist, call `reckon-create` first.

## Hard rules

1. **The plan HTML is the source of truth.** Read `docs/<slug>.html`. Do not
   implement items marked "deferred", "post-v1", or behind an unmet trigger.
2. **Multi-item sections get a fleet.** ≥ 3 independent items → one worker per
   item, dispatched in parallel, with non-overlapping file scopes.
3. **Scope allocation precedes dispatch.** Write down each worker's exclusive
   write paths before sending a single prompt. No two workers share a file.
4. **Parallel-safety preamble is mandatory in every worker prompt.** Embed
   the block verbatim (see §Worker dispatch boilerplate below).
5. **Audit every commit.** Run `git show --stat <sha>` against the declared
   scope. Surface violations in the final report; do not silently drop them.
6. **Per-stage HTML and a followup are required after every landing.** Even
   single-item work gets a `<slug>-<section>-landed.html` and a queued §05
   followup. Silence is not allowed.
7. **Collapse the evergreen when a section ships.** The evergreen page is a
   current-state dashboard, not a transcript. When a section lands, REPLACE
   the section body with a 2-4 line landed-summary + link to the per-stage
   HTML. Full prose lives in the per-stage record. Pending vs done must be
   visually distinguishable at-a-glance. See §5b.

## Workflow

### 1. Read the plan — classify items

Walk `docs/<slug>.html` section by section and decide:

| Signal | Action |
|---|---|
| Past-tense prose / commit SHAs present | Skip — already done |
| Marked "deferred", "v1", or "post-smoke" | Skip — respect timing |
| `Trigger:` subsection with unmet condition | Skip — surface to user |
| Concrete deliverable, no deferral signal | Implement |

Report the audit (implementable / deferred / blocked) to the user before
dispatching anything. Include the chosen model tier per item.

### 2. Scope allocation — assign exclusive write paths

List **exclusive write paths** per item before dispatch. No overlap. If two
items share a file, serialise them or split it (`test_a.py` / `test_b.py`).

### 3. Dispatch workers

| Implementable items | Strategy |
|---|---|
| 1 | One worker (or inline if tiny) |
| 2–8 | Parallel fleet, one per item |
| > 8 | Haiku reader fleet + Sonnet/Opus synthesiser |
| Cross-cutting / strategic | Single Opus |

Build each prompt from the §05 template below. Embed the parallel-safety
preamble verbatim. Include `Done-when` criteria and the followup requirement.

### 4. Wait and audit

For each returned worker:

1. Run `git show --stat <sha>` — confirm only assigned paths appear.
2. Run the project's test suite (`uv run pytest -q`, `ctest`, `npm test`, …).
3. Confirm the worker appended a followup to
   `docs/state/<project>/<slug>.json#followups[]`. If missing, write one.

### 5. Record outcomes

**Per-stage file** — create `docs/<slug>-<section>-landed.html`:
- Links to `_shared/foundation.css` and `_shared/dashboard.css`
- `<script src="_shared/state.js" defer></script>`
- Quick-status grid (shipped vs deferred)
- Outcomes table: item, badge, commit SHA, follow-up title
- "What's next" card pointing at the new followup

**Evergreen update** — see §5b for the binding collapse-on-landing rule.

### 5b. Collapse-on-landing — evergreen is a dashboard, not a transcript

This is the rule that keeps plans readable as they age. Once a section is
shipped:

**A) Move full content to the per-stage HTML** (`<slug>-<section>-landed.html`).
That file is the archival record — verbose, complete, immutable. It holds
the original prose, the decision rationale, code excerpts, screenshots,
debugging notes. Treat it like a git tag: write-once, read-forever.

**B) Replace the section body on the evergreen with a landed-summary card.**
The evergreen page now shows ONLY current state. The summary card has:

```html
<section id="s12-5" class="section-landed">
  <header>
    <span class="badge badge-shipped">✓ landed 2026-05-26</span>
    <h2>§ 12.5 — Bulk-encode rbb + magnetics</h2>
  </header>
  <p class="landed-summary">
    Encoded 11,237 shots (97% of training-grade corpus) on 4× H200 in 3h12m
    of GPU time. Visible-camera tokens at <code>/work/projects/imas_gpu/mast/tokens/rbb/</code>.
    Full outcome record:
    <a href="tokenizers-12-5-landed.html">tokenizers §12.5 landed</a>
    (commits <code>abc1234</code>, <code>def5678</code>).
  </p>
</section>
```

The landed-summary is **2-4 lines max**:
- Line 1: what was built, in past tense
- Line 2: quantitative outcome (number, percentage, bench score)
- Line 3 (optional): link to per-stage record + commit SHAs

**C) Visual rules:**
- Section header carries a `✓ landed YYYY-MM-DD` badge (`.badge-shipped` class).
- Body uses `.landed-summary` class (muted, italic, or whatever the design
  system specifies — see `~/Code/reckon/docs/_shared/dashboard.css`).
- The original prose is GONE from the evergreen — readers find it via the
  per-stage link, not by scrolling.

**D) What to keep visible on the evergreen:**
- Decision rows (locked or open) for that section — still load-bearing.
- Open followups referencing the section — still actionable.
- Tests pulse for the section — still drift-indicator.

**E) Trigger:** the moment the section's status flips from `active`/`in-progress`
to `shipped`. Don't collapse incrementally — collapse once, at landing.

**F) Dissent path:** if a reader thinks the collapsed summary lost something
load-bearing, they file a followup (`/reckon-edit <slug> --uncollapse <sec>`)
rather than re-expanding the evergreen unilaterally. The per-stage HTML is
always there to lift content from.

**Why this matters:** plans that don't collapse become unreadable scrolls
within 2-3 sprints. The evergreen should pass a 30-second-scan test: "what
is currently in flight, what is locked, what is pending". Detail on shipped
work has a different audience and belongs in the per-stage archive.

### 6. Update state and write followup

POST resolved decisions to the state API (see §State write pattern). If the
project uses central-index, update `index.json` — set `status: "shipped"`,
append commit SHAs to `evidence[]`, update `implementation_fraction`.

Write (or confirm) the §05 followup via `reckon-edit` or directly. If this
run was triggered by a followup, mark that followup resolved with
`resolved_at`, `resolved_by`, and `outcome`.

Commit the coordinator delta:

```bash
git add docs/<slug>.html docs/<slug>-<section>-landed.html \
        docs/state/$PROJECT/<slug>.json
git commit -m "docs(reckon): <slug> §<section> landed — <one-line summary>"
git pull --no-rebase origin <branch>
git push origin <branch>
```

## Dispatch prompt template (§05)

Embed this verbatim in every worker prompt, substituting angle-bracket fields:

```
Project: <project-name>
Plan:    <slug> (<url>)
Section: <§N — section title>
Tier:    <haiku | sonnet | opus>

Context
  <2–3 sentences: what this section does and why it is being shipped now>

State to read
  docs/state/<project>/<plan>.json

Locked decisions to honour
  <key> → <choice>
  ...

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
  3. followup written + driving followup resolved
```

## Worker dispatch boilerplate

Embed this block **verbatim** at the top of every worker prompt:

```
PARALLEL-SAFETY RULES (binding — violating any is a hard failure):
1. Stay on branch `<BRANCH>`. Never checkout or create branches.
2. `git stash` is BANNED. Commit your files instead.
3. `git add -A` / `git add .` / `git commit -a` / `git commit -am` are BANNED.
   Required workflow:
     git status --short                # confirm only YOUR paths are dirty
     git add <explicit path list>      # never -A / . / *
     git commit -m "..."               # never -a / -am
     git pull --no-rebase origin <BRANCH>
     git push origin <BRANCH>
4. If any path outside your exclusive scope is dirty, STOP and report.
   Do not stage it; do not stash it.
5. Your final report MUST include `git show --stat <sha>`.

YOUR EXCLUSIVE WRITE SCOPE (stage ONLY these):
  <path 1>
  <path 2>

CONCURRENT WORKERS (do NOT touch their scopes):
  Worker B: <paths>
  Worker C: <paths>
```

Append the item-specific task body. End every worker prompt with:

```
FOLLOWUP REQUIREMENT (binding):
After tests pass, append an entry to docs/state/<project>/<slug>.json#followups[]:
{
  "id":               "f-<timestamp-base36>",
  "written_by":       "<worker name>",
  "written_at":       "<iso-now>",
  "title":            "<imperative one-liner>",
  "body":             "<2–3 sentences on what's next>",
  "recommends_skill": "/reckon-ship <slug> [section]" | "/reckon-edit <slug>" | null,
  "touches":          ["<file>"],
  "tier":             "haiku" | "sonnet" | "opus",
  "est_turn":         "~1h" | "~1d" | "~1 sprint",
  "prompt":           "<§05 template body, ready to paste>"
}
Then mark your driving followup resolved (resolved_at, resolved_by, outcome).
If nothing follows, set prompt = "done — no followup".
```

## State write pattern

Always read current state before writing to avoid 412 conflicts:

```bash
ENVELOPE=$(curl -s "http://127.0.0.1:8765/state/$PROJECT/$SLUG")
CUR_VER=$(echo "$ENVELOPE" | jq -r '.data._version // 0')
NEW_DATA=$(echo "$ENVELOPE" | jq '.data // {} | del(._version)' | jq \
  --arg key "<decision-id>" \
  --arg val "<choice>" \
  '.[$key] = {choice: $val, when: "'"$(date -u +"%Y-%m-%dT%H:%M:%SZ")"'"}')
curl -s -X POST \
  -H 'Content-Type: application/json' \
  -H "If-Match: \"$CUR_VER\"" \
  -d "$NEW_DATA" \
  "http://127.0.0.1:8765/state/$PROJECT/$SLUG"
```

Always use `If-Match` — omitting it causes HTTP 412.

## Model selection

| Work type | Tier |
|---|---|
| C++, Fortran, solver physics | opus |
| Python, docs, config, test additions | sonnet |
| Research, file audits, inventory reads | haiku |

When in doubt, escalate upward. A wasted Sonnet is cheap; a Haiku silently
breaking a build is not.

## Cross-references

- `~/.claude/skills/reckon-edit/SKILL.md` — how the evergreen plan gets its
  "landed" subsection and how followups are written
- `~/.claude/skills/reckon-create/SKILL.md` — first-time plan scaffolding
- `~/.claude/skills/reckon-status/SKILL.md` — read-only inspection before
  deciding what to ship
- `~/Code/reckon/docs/_shared/` — shared CSS and state.js assets
