# Agent Guidelines — reckon

> Shared guardrails live in `~/.agents/AGENTS.md`. This file covers repo-specific
> rules every worker needs; reference material lives at the narrowest scope that
> owns it, loaded automatically when work happens there.

## Scope pointers

- Crew dispatch, the command surface, and fleet sizing: see `reckon/crew/AGENTS.md`
- Plan format, layout, and server operations: see `docs/AGENTS.md`

## Project

**reckon** is a repo-agnostic agile planning system. Primary branch: `main`.

The repo provides:
- `reckon/serve.py` — Python HTTP server for serving plan docs and state (port 8765 by default)
- `docs/` — Canonical React/JSX SPA for browsing, navigating, and acting on plans

## Python

- Package manager: uv (`uv run reckon serve` to start the server)
- Python ≥ 3.12, dynamic versioning via hatch-vcs
- Tests live under `tests/`, run with `uv run pytest`

### Ruff compliance is the target state

This project keeps `uv run ruff check` and `uv run ruff format --check .`
clean. That is the goal and not yet the measurement — there is a real backlog,
currently a few hundred findings with most autofixable and roughly a third of
the files unformatted. Measure it, never quote a figure from here: the count
moves with every commit, and a number written down in a document is the fuse
described under *A test must not encode the current date*. Treat the two
obligations below as separate, because conflating them is how the backlog
first got in.

**Your commit adds no findings.** Lint and format only the paths it stages,
never `.` — the whole-tree fixer rewrites files you did not touch, and in a tree
with this backlog it will. The general rule, the reason, and the runnable
before/after count live in `~/.agents/AGENTS.md` under *Pre-Commit Hook Policy*.
A pre-existing finding in a file you edited is not yours to fix in passing.

**Clearing the backlog is its own commit, and nothing else is in it.** One
`style(lint):` commit per sweep, no behaviour change, suite green either side,
and sequenced when no peer holds uncommitted work — a sweep touches ~50 files
and will collide with anything in flight. Several sessions commonly share this
checkout, so check `git status` and ask on the peer socket before starting one.

**The rule set is declared, not inherited.** `[tool.ruff.lint]` in
`pyproject.toml` carries the selection, and the dependency carries an upper
bound for the same reason: an inherited default makes "compliant" mean whatever
the installed ruff happens to check, so an upgrade would redefine the contract
for everyone at once with no code change. Every entry in `ignore` names a
pattern this codebase uses deliberately — asserts in tests, subprocess-driven
tooling, lazy imports, long refusal messages, stdout as the CLI's interface.
Adding one because a finding is tedious to fix is how a lint config stops
meaning anything; that belongs in the backlog. Changing either list is a
deliberate edit with its reason in the comment beside it.

## Tests

In a detached worktree the shared uv cache may be mounted read-only, and
`uv run` then dies before pytest collects anything. Call the root
environment's interpreter directly instead — it needs neither the cache nor a
sync:

```bash
PYTHONPATH="$PWD" <repo>/.venv/bin/python -m pytest -p no:cacheprovider -q
```

Judge a run by its **delta against a known base revision**, not by an absolute
count. A worker sandbox restricts sockets, config-home writes and network
builds, so a tier that cannot execute freely reports failures that have nothing
to do with the code under test.

### A test must not encode the current date

The sibling of the rule below, and it fails the same way: silently, later, and
for everyone at once. A test that hardcodes a value derived from *today* —
an age in days, a computed deadline, a formatted timestamp — passes on the day
it is written and every day until it doesn't. Measured here on 2026-08-25: an
archive dry-run test asserted `"145d"` and `"176d"`, ages derived from its own
fixture dates and frozen into literals. It passed its node's gate, passed the
full suite twice, and turned `main` red at midnight with nobody touching the
code. Two workers in another session were blocked on it before anyone noticed,
because the failure surfaced in *their* gate rather than in ours.

Derive the expectation from the same fixture the code sees, at assertion time.
If a test needs a fixed age, compute it from the fixture's own date rather than
writing down what that age happened to be. The tell is a literal that would
have to be edited on a future date to keep the test true.

The same applies to any recorded absolute that moves on its own — a pass count
baked into a done-when, a line number in a comment, a total that grows. State
the delta or derive the figure; a snapshot of a moving number is a fuse.

### A test must not read or write state outside the repository under test

A test that does is not a test, it is a monitor: it reports on the machine
rather than on the code. It also passes when written, which is why review never
catches it. Both directions have been measured here, and they are not equally
bad:

- **Reading** outside state makes the test wrong whenever the environment
  moves. Preflight assertions naming absolute paths in sibling checkouts failed
  the moment those directories were legitimately removed — the test broke on an
  authorised action, not on a defect.
- **Writing** outside state makes *someone else* wrong. A dispatch test wrote
  its live run pointer into the real pointer directory, so two concurrent test
  runs collided with each other, and a test could corrupt a live fleet
  session's view of what is running.

The write direction is the dangerous one: a reader can only be wrong, while a
writer can make others wrong. So synthesise a temporary repository rather than
reaching for a real one, point every environment-resolved directory at a temp
path — and then assert the real directory is untouched afterwards, because an
isolated read does not prove an isolated write.

## Frontend

The docs/ directory is the canonical planning SPA template:
- Pure client-side React 18 + JSX compiled in-browser via Babel standalone (no build step)
- CSS: docs/_shared/foundation.css, docs/_shared/dashboard.css
- JSX components: docs/ui/ (shell.jsx is the root)
- Plan state is loaded at runtime from the plan's semantic HTML elements (parsed by the server via `GET /plan/<project>/<slug>`); project config from `state/<project>/index.json`

### The SPA renders; it does not derive (binding)

**One functional source of truth.** Any derived fact — a sprint's real
completion, an endpoint's closure, a project's rollup, a schedule, a blocker, a
readiness verdict — is computed in Python and consumed by the surface. The SPA
renders the payload. It never computes a fact a reader without a browser would
also want.

The test is one question: **would an agent, a CLI caller, or an MCP read have
any use for this value?** If yes, it belongs in Python and the SPA reads it. If
the value is presentation — a card width, an edge path, a lane height, a colour
— it belongs in the surface and has no agent-side counterpart.

Why it is binding rather than advisory. A derivation written in JSX is invisible
to every non-browser reader, so a human and an agent reading the same project
disagree and neither finds out. Measured 2026-09-04, immediately after the
surface work landed: `roadmap` had no notion of derived sprint state or of an
unnamed dependency endpoint, and nothing outside the HTTP handler could call the
fleet rollup, while the SPA answered all three. A mirror implementation is not
the fix — that is two implementations of one truth, which is the same defect
with a longer fuse, because nothing fails when the copies drift.

Two consequences worth knowing before you choose:

- **Verification gets cheaper.** Logic in Python is asserted by pytest. Logic in
  JSX needs a headless browser or a module eval, which is the heaviest and least
  reliable test path in this repository.
- **Deployment gets slower.** A JSX change is live on the next page load; a
  Python change needs the served process restarted and the MCP reconnected
  before any reader sees it. That is the real cost of centralising, and it is
  worth paying for a fact, not for a pixel.

Parity is held by a check that compares the surfaces rather than asserting
about either one, because literals on both sides drift silently in step.

## Repo-agnostic principle

Never hardcode a project name (imas-ambix, imas-efit, etc.) in reckon itself.
Project identity comes from `meta[name="docs-project"]` in the served HTML and from mounts.json.

## HTML-first plans

Plain markdown remains fine ONLY for short prose (READMEs, brief notes).
Anything project-level that needs tables, diagrams, status tiles,
side-by-side comparisons, interactive decision capture, or shared
state across humans and agents is a **plan**, and plans are HTML.

**Non-plan structured docs are ALSO HTML.** RCAs, incident reports,
SDCC/ops tickets, design reviews, explainers and dashboards are NOT
markdown — author them with `reckon-create` using `reckon-type=doc`
(a standalone HTML page in `docs/`, no plan lifecycle). If you catch
yourself writing a `docs/*.md` for anything with a table, a timeline,
or a status, stop and use `reckon-create` instead. (This rule exists
because an RCA + an SDCC ticket were authored as markdown on 2026-05-27
when the routing table still pointed at a since-removed `html-docs`
skill — they had to be re-authored as HTML.)

**Reckon MCP server down is NOT an excuse for markdown.** `reckon-create`
writes the HTML file directly from its template; the server is only
needed to mutate plan *state* (status/impl/decisions/followups) later.
When the MCP is down: still author the HTML doc/plan, and apply state
mutations via MCP once it reconnects (or note the deferral).

### Skills you must use — never freelance

Plans have a specific skill set. Do not edit `docs/*.html`
without invoking the matching skill first; the skills bake in rules
that ad-hoc edits routinely violate (cumulative evidence, state
writeback, content parity, fleet safety).

| Intent | Skill | Slash command |
|---|---|---|
| Create a brand-new plan | `reckon-create` | `/reckon-create <slug>` |
| Edit an existing plan, lock a decision, record an outcome, write a followup | `reckon-edit` | `/reckon-edit <slug>` |
| Implement the work a plan describes; record outcomes; followup with §05 invocation | `reckon-ship` | `/reckon-ship <slug> [section]` |
| Sprint / milestone / roadmap state (the project index) | `reckon-sprint` | `/reckon-sprint` |
| Pure-read inspection across all plans in this repo | `reckon-status` | `/reckon-status` |
| Pending work, true blockers, critical/open paths, and DAG wiring | `reckon-roadmap` | `/reckon-roadmap` |
| Set up or refresh reckon infra in a repo (CSS, mounts, state dir, symlink) | `reckon-sync` | `/reckon-sync` |
| Non-plan docs (RCAs, incident reports, tickets, reviews, explainers, dashboards) | `reckon-create` (with `reckon-type=doc`) | `/reckon-create <slug>` |

**Trigger discipline.** When the user says "the X plan", read the
relevant skill's SKILL.md **before** touching any file. The skills
live at `~/.claude/skills/reckon-*/SKILL.md`. They are short; reading
them is cheap.

### Plan-state integrity (mandatory — fix for the "silent bypass" failure mode)

**Failure mode that motivated this section.** 2026-05-21: shipped
plans-infra in dotfiles + ambix without updating the plan's state
to reflect that it shipped. The plan-system told a different story
than the codebase. RCA: the coordinator authored a custom sub-agent
dispatch prompt instead of routing through `/reckon-ship` (which has
the followup-write requirement baked in), AND failed to write a
closing followup on the parent plan when work completed.

**The mandate.** Any change to a plan's state — implementation lands,
decision resolves, status changes, blocker clears, sprint moves —
MUST be reflected in the **plan's semantic HTML** (via MCP tools or
`POST /plan/<project>/<slug>`) **in the same turn that the change
happens**.

**The HTML is the sole store.** There are no per-plan
`state/<project>/<slug>.json` sidecars. All plan data (status, impl,
decisions, followups) lives as `<meta name="plan-*">` scalars and
`data-reckon` section elements in the plan HTML. The server rewrites
those elements on every successful POST.

Concretely:

1. **When work lands on a plan**:
   - `status` updated (`active` → `shipped`/`done` when fully done;
     `active` → `blocked` when stalled; etc.)
   - `impl` advanced toward 1.0
   - `modified` set to today (server-written on each POST)
   - **The driving followup MUST be resolved** with `resolved_at`,
     `resolved_by`, `outcome` describing what landed
   - **A new followup MUST be written** with the one-line §05 invocation
     for whatever comes next — or with `outcome: "done — no followup"`
     when the chain truly closes
   - `version` will be incremented by the server on each POST (do
     not set it client-side)

2. **When a decision is resolved**:
   - `decisions.<key>.choice`, `rationale`, `when`, `by` updated
     via the MCP `edit_plan` `lock` op or `POST /plan/<project>/<slug>`
     with a dotted patch. The server sets `data-choice` on the `.r-dec`
     element in the HTML. Direct HTML edits are permitted ONLY if you
     announce the bypass reason in your reply.

3. **Coordinator dispatch contract.** When dispatching any worker to
   implement plan work, the dispatch prompt MUST:
   - Identify the live plan and section as the semantic authority; do not copy
     plan guidance into the handoff
   - Include only runtime safety data that cannot live in the plan, such as
     worktree, exclusive file scope, manifest path, model/effort, and
     concurrency peers
   - Require the worker to return outcome evidence to the coordinator; workers
     do not edit shared plan state

   When all workers land, the **coordinator MUST resolve the driving followup
   and write the next one-line invocation** before marking the task shipped.

4. **Eat-the-dog-food check.** Before marking any reckon-ship work
   "done" in chat, verify the plan-system itself reflects the work:
   - `GET /plan/<project>/<slug>` → `status` matches reality
   - The driving followup is resolved (`data-status="resolved"` on its `<article class="r-fu">`)
   - A next followup or `outcome: "done — no followup"` is present
   - `version` has incremented
   If any of those is false, you bypassed the skill. Fix it before
   moving on.

5. **State-bypass announcement.** Any agent that edits plan HTML
   elements directly (rather than via MCP tools or `/reckon-edit`)
   MUST announce "bypassing /reckon-edit because X" in their reply.
   Silent bypasses are exactly the failure mode being prevented here.

This is enforced by discipline, not by tooling — but the discipline
is binding. Surface a violation explicitly when you spot one.

## Plan Lifecycle Invariants

The `plan-lifecycle-hygiene` review proposed six lifecycle-hygiene mechanisms.
Treat them as the operating checklist below until the remaining design-review
questions are resolved.

1. **Write-time lifecycle invariants** — **pending lead design review**.
   Do not add hard/soft write-time enforcement for `impl`, landed summaries, or
   research-doc closure until the lead locks the strictness choice.
2. **Audit cadence** — **implemented** via `reckon audit`.
   Run `uv run reckon audit` (or `uv run reckon audit --project <name>`) to scan
   mounted projects for:
   - `STALE`: active plans older than 30 days with `plan-impl < 1.0`
   - `MISSING_IMPL`: shipped/done plans with missing or zero `plan-impl`
   - `STALE_RCA`: `reckon-type=research` docs older than 60 days whose status is
     not `done`/`archived`
   The command exits with code 1 when any `MISSING_IMPL` row is present, so it is
   safe to wire into CI or a weekly hygiene job.
3. **Archive waves** — **policy active; automation pending**.
   Run a quarterly archive pass that sets `plan-archived=1` on done or
   superseded docs older than 90 days. Execution outcomes accumulate in one
   coherent `docs/evidence/archive/<slug>-landed.html` record per plan by
   default. Do not create section-sized evidence files unless the artifact is
   independently useful; the default dashboard should stay focused on live docs.
4. **Plan-creation diet** — **pending lead design review**.
   Do not tighten `reckon-create` pre-flight behaviour until the lead decides how
   hard to bias new work toward existing plans.
5. **Sprint-close gate** — **pending lead design review**.
   Do not add close-sprint refusal logic for unresolved followups / unset impl /
   missing landed summaries until the lead chooses the enforcement mode.
6. **Backfill wave** — **groundwork only**.
   Use `reckon audit` output to identify candidate stale docs, but defer any
   fleet-scale backfill pass until milestones 1/4/5 are reviewed and approved.
