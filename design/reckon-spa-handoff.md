# Handoff — landed SPA vs. designed SPA

Source of truth for the design: `Reckon SPA v2.dc.html` in the Claude Design project
`Reckon SPA design review` (`4d84ed98-9413-4d4e-8eb1-1a641b338b24`), mirrored into
`design/` in this repo. This file triages what the landed implementation
(`docs/ui/*` @ main) does versus what the design specifies.

Read order for an implementer: this file, then `design/reckon-spa-handoff.dc.html` (the design project's `Reckon SPA v2.dc.html`; the path is the one edit made on import).

---

## A · Defects — implementation bugs, not design gaps

### A1 · Graph tab renders an empty state where a handle is not on the endpoint
**Severity: blocking. Correction — an earlier draft of this file said no plan
carries a `graph_handle`. That is wrong.** Three live handles exist:

| Handle | Endpoint | Repo | Members |
| --- | --- | --- | --- |
| `sprint-federation` | `sprint-scope-and-surface` | reckon | 5 |
| `hexgrid` | `single-grid-solver-cutover` | nova | 4 |
| `west-rc` | `sn-west-catalog-release` | imas-codex | — |

`docs/ui/graph.jsx` → `_graphHandleView()` still returns `null` unless the
*selected endpoint* carries one, and `DependencyChainView` then renders the
empty-state paragraph as the entire surface. `_allDependencyChains()` builds
endpoints as `[...handled, ...live]`, so a live-but-unnamed endpoint can be
selected and blanks the tab — which is what the nova screenshot shows.

**Fix:** derive the view for an unnamed endpoint too — handle chip reads
`unnamed`, and closure membership, derived authority, metrics and canvas all
render exactly as they do for a named one. Reserve the empty state for a project
where every plan genuinely stands alone. The design does this and reproduces
`sprint-federation` at 5 of 5 members.

**Ship targets are not invented.** `skills/reckon-ship/SKILL.md` defines three
and only three: `/reckon-ship <slug>` (single plan), `/reckon-ship S1`
(sprint), `/reckon-ship graph:<handle>` (closure). There is no `--closure`
flag and a project name is not a target. So:
- a named endpoint ships as `/reckon-ship graph:<handle>`;
- an **unnamed** endpoint is not shippable as a graph at all — the surface shows
  the missing precondition (`needs plan-graph-handle`, dashed amber) instead of
  a copy button. This is deliberate: it names the one authoring act that would
  make the trajectory dispatchable;
- a project with **no active sprint** gets no sprint ship control, rather than
  a fallback target that cannot resolve.
An unresolvable string in a copy-to-clipboard control is worse than no control,
because its whole value is being paste-ready.

### A2 · Hidden projects still appear in the project picker
`docs/ui/shell-topbar.jsx`. The dropdown maps `manageableProjects`
(= *all* projects, `manageableProjectRows` is identity) while visibility is
computed by `visibleProjectRows()` over `mountedProjectRows()`
(= `plans_count > 0`). Hidden rows are therefore rendered with a `hidden`
suffix instead of being removed.

**Fix:** render `visibleProjectRows(...)` in the picker. The full list belongs
only in the visibility configurator.

### A3 · Visibility cannot actually be toggled
Two causes, both in `shell-topbar.jsx`:

1. `mountedProjectRows()` filters on `Number(project.plans_count) > 0`. In the
   served state most projects report `plans_count: 0` (the screenshot shows
   `nova · 0 plans · 3 live` while its Plans tab lists 80). Those rows are never
   "mounted", so `visibleProjectRows()` never contains them and
   `projectVisibilityChange()` computes `survivors` from an empty-ish set.
2. `projectVisibilityChange()` returns `{changed:false}` whenever hiding would
   empty the survivor list — correct as a last-one guard, but it fires far too
   often because of (1), so toggles silently no-op.

**Fix:** (a) fix `plans_count` at the source (`reckon/project_state.py` fleet
index) — a project with plans must not report zero; (b) stop using
`plans_count` as the mount predicate — mount = registered; (c) keep the
last-visible guard but make the refusal *visible* (the design locks that row and
labels it `locked` rather than ignoring the click).

### A4 · Crew floods and cannot scroll
`docs/ui/crew.css` sets `.r-crew-view { flex:1; overflow:auto }`, but an ancestor
in `shell.jsx` has no `min-height: 0`, so the flex item grows past the viewport
instead of scrolling. At 1374 the list is clipped entirely (see
`docs/figures/spa-surface-redesign/after/crew-1374.png`).

**Fix:** `min-height: 0` on every flex ancestor between the app root and
`.r-crew-view`. In the design every surface is
`flex:1; min-height:0; overflow-y:auto` by construction.

### A5 · Crew is not repo-scoped
`shell.jsx:326` passes `visibleProjects={shownProjectNames}` — Crew shows *all
visible* projects while the topbar says `nova`. The screenshot shows an
`imas-ambix` run under a `nova` selection.

**Fix:** scope Crew to the selected project by default, with an explicit
"All visible" toggle. Same rule for every surface: **the project selector scopes
the page**. Only the fleet home is cross-project.

### A6 · New content requires a page refresh
Plans, research and evidence added to the repo only appear on reload. `/crew` is
polled every 3s (`sprint.jsx`, `crew.jsx`) but the plan inventory is loaded once
by `docs/ui/state-loader.js`.

**Fix:** poll the state index on the same interval and diff by
`resource_versions`. Arrival must be *non-destructive*: never re-sort or
re-scroll the list under the reader. The design specifies:
- a `live` pill in the topbar that becomes `N new`;
- a banner at the top of the affected list — *"3 new evidence since you opened
  this list · show"* — that inserts on click;
- a one-shot tint (`animation: arrive`) on inserted rows;
- a count badge on the affected tab.

### A7 · Sprint horizon shows no work
`sprint.jsx` → `horizonStrip()` plots only run events with a parseable
`completed_at`/`dispatched_at` inside a fixed 48h window. The screenshot reports
`0 timestamped events` against an active sprint with 20 sprints of state — so
either the run ledger is not being written with timestamps, or the finished-runs
fetch (`/crew/<project>/finished/<slug>`) is failing silently (its `.catch()`
sets `error` but the overview strip does not surface it).

This is partly a write issue and partly the wrong model — see **B1**.

---

## B · Design changes — the landed surface is working as built, and built wrong

### B1 · Sprints: schedule from the graph, not from dates
**Landed:** a fixed 48-hour wall-clock strip with hour ticks, plus a table.
Work is placed by run timestamp only, so a sprint with no dispatched runs is an
empty strip. Sprint `starts`/`ends` are authored dates.

**Designed:** a *derived flow*. Nothing is scheduled to a date.
- x-axis is **hours relative to now**, not clock time. Ticks are `-48h … now …
  +72h`, and the extent is derived from the data, not fixed.
- Left of the now-line: **recorded** work, placed from the run ledger, and
  **in-flight** work, placed from `dispatched_at` + elapsed.
- Right of the now-line: **predicted** work. A plan's bar starts at
  `max(end of its prerequisites, 0)` and runs for its `wall_clock_hours`.
  Nothing else determines position.
- Lanes come from best-fit packing of non-overlapping bars. **Lane count is the
  realisable width** — the number the fleet can actually absorb.
- Move a dependency or re-estimate an hour figure and the tail re-hangs. That is
  the whole point: the timeline is a *view of the graph*, not a plan of record.

Everything needed is already computed server-side:
`reckon/roadmap.py` has `_effort_hours()`, `_wall_clock_hours()` and
`critical_path`; sprint items already carry `effort_hours` in
`data-item` (see `docs/sprints/S13.html`, `S14.html`). The SPA does not consume
any of it. Serve `effort_hours`, `wall_clock_hours`, `depends_on` and the run
ledger's timestamps on the inventory rows and the flow computes client-side.

Header figures the design exposes: *worker-hours left*, *realisable width*,
*critical chain (h)*.

### B2 · A sprint must open its plan graph
**Landed:** sprint rows link to `#sprint/<id>`, which re-renders the same
all-sprints overview. There is no way to see a sprint's DAG.

**Designed:** clicking a sprint row opens a sprint detail surface whose body
*is* the DAG.
- Columns are dependency depth; cards are plans with status, implementation bar,
  slug and worker-hours.
- **Prerequisites outside the sprint are included as dashed context nodes.**
  Without them S14 is four unconnected cards; with them it reads as a chain.
  This is the single most important detail — a sprint DAG drawn only from sprint
  membership is not informative.
- Edges: solid where the prerequisite has shipped, dashed where it has not, red
  where a `blocked` plan hangs off an unshipped prerequisite.
- Header: plans · worker-hours · depth · held · open decisions, and a ship line
  that is *disabled while decisions are open* (`N open decisions`, not a copy
  button that hands you a poisoned prompt).

### B3 · Research, Evidence and Figures are first-class tabs
**Landed:** research and evidence exist only as a rail inside the plan reader,
so a new artifact can only be found by knowing which plan it hangs off.

**Designed:** four sibling artifact tabs — `Plans · Research · Evidence ·
Figures` — sharing one layout (filter header, list, reader). Differences from
the Plans list:
- every row shows **created** and **edited** explicitly, and the header carries
  an Edited/Created sort toggle that changes the sort key, not just the label;
- each list is a **feed**: newest first by default, with the arrival mechanics
  from **A6** so a document written by a worker shows up while you are looking
  at the page;
- Figures rows carry a thumbnail and dimensions; the reader shows the image with
  its `docs/figures/...` path underneath.

### B3a · Reader chrome: source trail, full screen, prev/next
All four artifact types share one reader. Three behaviours it must carry:

**Source trail.** The old path chip and title are replaced by one breadcrumb:
`S14 / Redraw the Degraded Surfaces / Graph surface at 1920 — after`. A figure
links to its plan (`forPlan`), that plan to its sprint; evidence the same;
research via the first entry in `informs`; a plan shows only its sprint. Every
segment except the last navigates. Without this, opening a figure strands you.

**Full screen (`f`).** Drapes over the *entire shell*, not just the reader pane
— fixed inset 0 above the topbar. Rationale: every topbar control navigates away
from the document, and the left rail is the list you are already stepping
through. `esc` or the chip exits. Implemented as a mode on the existing reader
(one style swap), not a second copy of the markup.

**Prev/next.** `‹ 3 / 22 ›` plus `←`/`→`, in both normal and full-screen
modes, for all four types. Critical detail: the buttons, the keys and the
position readout must all read the **one** ordered list the left rail rendered —
including the active status filter and the Edited/Created sort. Deriving the
list a second time inside the step handler is how it silently breaks. Suppress
keys while the palette input has focus.

### B3b · Dependency cone is opt-in, and plan edges only
The always-on dependency graph at the top of the plan reader is replaced by a
toggle in the reader header labelled with its own counts (`2 ↑ 1 ↓`, or
`standalone` and dimmed when the plan is isolated). It opens a three-column
cone inline above the body.

**Only `depends_on` and its reverse.** Research and evidence are provenance —
they say why a plan exists and whether it worked, not what must land first.
Mixing them in is what made the old cone unreadable: a plan with four evidence
docs looked more entangled than one with four real prerequisites. They stay in
the Provenance list at the foot of the reader. The Graph tab's closure uses the
same edge rule, so the two cannot disagree.

### B3c · Graph rendering rules (both the sprint DAG and the Graph tab)
One shared layout helper. Two rules that are easy to get wrong and were both got
wrong in review:
- **Skip-level edges must route clear of the columns they cross.** An edge
  spanning more than one depth drawn as a straight cubic passes underneath the
  intervening column's opaque cards and reads as a chain that does not exist.
  Emit an explicit path — quarter-turn down, straight run *at* the clearance
  depth, quarter-turn up. Do not rely on a cubic's peak: with both control
  points at `yD` a cubic only reaches about ¾ of that offset.
- **Co-terminating edges must fan on arrival**, or two arrows into the same plan
  are indistinguishable. Offset each incoming edge's endpoint around the card's
  vertical centre.
- Stage height derives from the deepest detour actually emitted.

Data already exists: `docs/research/*`, `docs/evidence/*`, `docs/figures/*`,
with `informs` / `evidence_for` / `verifies` edges parsed in `graph.jsx`.
It needs to be served as top-level inventory rows with `type`, `created` and
`edited`, not only as nested plan children.

### B4 · Fleet home
**Landed:** `docs/home.html` + `docs/ui/home.jsx` — a scrolling list of all 13
registered projects, most of them `0 plans · 0% shipped · 0 updates`. The four
real projects are below the fold behind nine dead rows. Sparklines render flat
whether the series is empty or genuinely flat, so they read as broken.

**Designed:** a read-only oversight dashboard, in this order.
1. **Eyebrow** — date, `N of M shown`, `configure` link.
2. **Stat band** — projects moving · plans · active · in flight · held · shipped,
   at 26px. These figures *are* the headline; there is no headline sentence.
3. **Project table** — one row per moving project: live dot and last edit,
   30-day plan-edit streakline with `N edits · M last 3d`, a status composition
   bar, plan count and held count, active sprint. Projects with no recorded
   work collapse into one line that **hides entirely at zero**.
4. **In flight** and **Just landed** side by side.

Three rules this surface must keep:
- **No action queue.** This is a planning surface and is read-only; a
  "waiting on you" list implies resolution happens here. Held counts and open
  decisions surface as *figures* on the rows and in the sprint header instead.
- **Everything obeys global visibility.** The stat band, the table, in-flight
  and just-landed all filter through the one visible set — not just the table.
- **An empty series is not a flat line.** A project with no activity prints
  `no recorded activity`; it does not draw a zero-height polyline.

### B5 · Topbar order
Search moves to the left, beside the brand; the project selector moves to the
right, beside settings. Rationale: search is a global verb and belongs with the
identity; the project selector is a scope control and belongs with the other
scope/appearance controls it governs.

### B6 · Visibility configurator
A sheet, not a scrolling section of the settings menu. Every registered project
is a row with a real switch, its plan and live counts, and a state word. The
last visible project is `locked` and says so. Copy states the consequence:
*hidden projects leave the picker, the crew feed and the fleet roll-up;
registration is unaffected.*

### B7 · Palette
Searches all four artifact types, not just plans. Placeholder copy names them.

### B8 · Brand mark
The `r` tile and the "reckon" wordmark are replaced by a single compass-rose
mark on the ink tile at 27px: ring, needle rotated off-axis so it reads as a
bearing rather than an arrow, north solid and south at 38%, punched-out hub.
No wordmark. It is the home button.

### B9 · Language and ink
Two standing rules for the whole surface, applied throughout:
- **No explanatory prose blocks.** The design carries the meaning. Removed: the
  sprint-flow subtitle and footnote, the graph authority note, the visibility
  sheet explainer, the "click a sprint" hint. Colour keys and column legends
  stay — they decode an encoding rather than explain a concept.
- **No redundant ink.** A panel header that restates the page title, a column
  header whose cells are self-describing, a per-row legend that restates a bar,
  and a control whose count is permanently zero are all defects. Hide a control
  when its count is zero rather than rendering it dead.

---

## C · Visual
Keep the design's white ground (`--bg:#ffffff`) and type ramp
(Geist / Geist Mono) — not the landed cream `#fbfaf7`. Tokens are declared at
the top of `Reckon SPA v2.dc.html` and are a superset of the landed
`docs/_shared/foundation.css` names.

---

## D · Still open (design decisions, not defects)
- A plan in two G-closures: union the authority, or refuse the overlap?
- G scope escalation when a new upstream repo joins a closure: silent, or
  surfaced as a diff the human acknowledges?
- Reading full-screen with the list hidden: does the filtered list ghost in on
  hover, or stay hidden until ⌘B?
- Cross-repo sprint scope: force multi-project, or keep focus + a repo badge?
- Settings beyond visibility (theme, density, snapshot) is undesigned.
- Wiring the `G` resource type into reckon itself (closure, membership, ship
  path) is separate work and not covered here.


---

## E · Landing readiness — data contracts

The design is complete and internally consistent. Whether it *lands* depends on
what the served state carries. Checked against `main` @ `9cfde98`:

**Verified present**
- `plan-depends-on` / `plan-informs` meta on plans — the graph is real and the
  design's fixture now mirrors it edge for edge (`registered-repo-state-migration`
  has a genuine four-way fan-in; `flight-control-config → uniform-worker-dispatch
  → crew-run-ledger → {effort-calibration, inflight-visibility, budget-aware-dispatch,
  promotion-consequence-gate}` is a real depth-4 chain).
- `plan-graph-handle` — three live handles, see **A1**.
- `effort_hours` on sprint item contracts (`docs/sprints/S13–S15` `data-item`),
  and `_effort_hours()` / `_wall_clock_hours()` / `critical_path` in
  `reckon/roadmap.py`.
- `created` — derived from git times in `reckon/serve.py:1085`; `roadmap.py:243`
  passes `created_at=plan.get("created")`. The Created/Edited sort is servable.

**Not verified — confirm before building**
1. Are `effort_hours` / `wall_clock_hours` / `depends_on` on the **inventory
   rows the SPA loads**, or only in the roadmap view? The derived flow (**B1**)
   needs them client-side.
2. Are research, evidence and figures served as **top-level inventory rows**
   with `type`, `created`, `edited`? Today they are reachable as nested plan
   children. The three new tabs (**B3**) need them as rows.
3. Do figures carry dimensions? `docs/figures/*/capture-index.json` and the
   `.geometry.json` siblings look like the source, but the field names are
   unconfirmed.
4. **A per-day plan-edit series does not exist and must be produced.** The home
   streaklines (**B4**) need ~30 daily counts per project. Git history can
   supply it; nothing serves it today. This is the one place the design assumes
   data the backend does not have.
5. `plans_count` is wrong in the fleet index (**A3**) — `docs/state/reckon/index.json`
   reports 12 for a project the Plans tab lists 80 for, and that file now carries
   a `superseded` marker pointing at the distributed resources. Confirm which
   resource the SPA should read before fixing the count.
6. Live arrival (**A6**) needs the state index polled and diffed by
   `resource_versions`; only `/crew` is polled today.

**Order of work.** A1–A5 are self-contained defect fixes and can land first,
against the current data. B1, B3 and B4 each depend on an unresolved item above
— settle 1, 2 and 4 before starting them.
