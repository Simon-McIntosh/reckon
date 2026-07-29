# reckon plan format

> Single source of truth for the format. The server (`reckon/serve.py`,
> `reckon/_plan_html.py`), the SPA (`docs/ui/*.jsx`), the MCP layer
> (`reckon/_store.py`, `reckon/mcp.py`), and the skills (`skills/reckon-*`)
> all assume exactly this. One format.

## Schema contract

**`docs/_shared/plan.schema.json`** is the machine-checkable authoritative
contract, **derived** from `reckon/_schema.py:PlanState` via
`python -c "from reckon._schema import write_json_schema; write_json_schema()"`.
It is never hand-edited. `tests/test_schema.py` asserts the committed file equals
the freshly generated schema, so drift is caught in CI. The schema version
(`SCHEMA_VERSION` / `$id`) is bumped on any breaking change. The prose below
describes the same contract; the JSON Schema is the machine-checkable source.

**Schema served live** at `/_shared/plan.schema.json`. When authoring with
`edit_plan`, call `read_plan(project, slug, with_schema=True)` to get the
schema + dos/don'ts injected inline.

## Core principle

**The data IS semantic HTML.** Everything lives inside the `.html` file as
ordinary HTML the reader can see — decisions, followups, questions, research,
comments — plus a few `<meta name="plan-*">` scalars. There is no embedded
data blob and no per-plan state sidecar. The reckon server reads these elements
for discovery and rewrites them in place on a live edit.

A project stores workflow state in independently versioned sprint, milestone,
blocker, and timeline resources. `docs/state/<project>/project.json` contains
identity/presentation settings only. A migrated `index.json` is retained
byte-for-byte as a compatibility snapshot and is never written again.

## Typed resource layout and identity

Canonical resources live under declared roots:

| Type | Live root | Completed records |
|---|---|---|
| plan | `docs/plans/` | `docs/plans/archive/` |
| research | `docs/research/` | `docs/research/archive/` |
| evidence | `docs/evidence/` | `docs/evidence/archive/` |
| sprint definition | `docs/sprints/` | `docs/sprints/archive/` |

Stable identity is `(project, type, slug)`, rendered as
`project:type:slug`; it never depends on a relative path. Infrastructure stays
under `_shared`, `_ui`/`ui`, `state`, `assets`, `images`, and `figures`.
The resolver also reads bounded flat and shared-archive paths so mixed
repositories remain usable. `reckon migrate-layout docs` performs the explicit,
idempotent migration; reads never move files.

## Artifact types

`<meta name="reckon-type" content="plan|research|evidence">` (default `plan`).

- **plan** — an actionable unit with decisions, followups, status, impl.
- **research** — a non-actionable *input*: a reference/finding/analysis that
  one or more plans build on. It carries prose + `<meta name="plan-informs"
  content="slug-a,slug-b">` listing the plans it feeds. It has no decision /
  followup workflow. Plans reference research via `plan-depends-on`, so the
  link is traversable both ways (a plan shows its research inputs; a research
  doc shows the plans it informs).
- **evidence** — a non-actionable verification record linked through
  `plan-evidence-for` and optional `plan-verifies` section anchors.
- **sprint** — an HTML sprint definition identified by `sprint-id`; actionable
  item state remains in the project index.

**Normalisation note:** authoring `reckon-type=doc` is accepted for RCAs,
explainers, and other non-plan docs. The schema normalises `doc`→`research` on
read (`_norm_type`), so `doc` is never the stored type — only `plan` or
`research` appear in parsed state. Both author- and agent-facing templates
may write `doc` for clarity; the parser handles it.

## Head scalars — `<meta name="plan-*">`

| Meta name | Author or server | Values | Notes |
|---|---|---|---|
| `docs-project` | author | basename of repo root | required-on-write |
| `reckon-type` | author | `plan` / `research` / `evidence` (author may write `doc` → normalised to `research`) | |
| `plan-slug` | author | kebab-case | optional; default = filename stem |
| `plan-title` | author | Title Case | required-on-write |
| `plan-summary` | author | one-line synopsis | |
| `plan-status` | author / set on transition | see enum below | |
| `plan-roi` | author | `high` / `mid` / `low` (author may write `med` → normalised to `mid`) | |
| `plan-effort` | author | `S` / `M` / `L` / `XL` | |
| `plan-milestone` | author | e.g. `M2` | |
| `plan-sprint` | author | e.g. `S4` | |
| `plan-capability-version` | author | `1.0` | persisted capability contract version |
| `plan-capability-class` | author | `routine` / `general` / `orchestrator` | broad dispatch floor |
| `plan-capability-reasoning` | author | `standard` / `deep` | optional hard floor |
| `plan-capability-context` | author | `standard` / `extended` | optional hard floor |
| `plan-capability-tool-autonomy` | author | `guided` / `autonomous` | optional hard floor |
| `plan-capability-verification` | author | `standard` / `strict` | optional hard floor |
| `plan-capability-risk` | author | `low` / `moderate` / `elevated` / `critical` | optional hard floor |
| `plan-owner` | author | free text | |
| `plan-depends-on` | author | comma-separated plan refs | dependency DAG; local **and** external |
| `plan-blocks` | author | comma-separated plan refs | reverse-dependency; local **and** external |
| `plan-informs` | author | comma-separated slugs | research docs only |
| `plan-archived` | author | `1` | hides plan from default inventory; use when retiring a plan without deleting it |
| `plan-read` | author | `1` | marks a research/doc as reviewed; no effect on plans |
| `plan-impl` | set by `reckon-ship` | `0.0`–`1.0` | progress fraction; `reckon-ship` sets it (shipped sections / total) on each landing. **Not** auto-computed by the server — leaving it unset keeps a plan at 0%. |
| `plan-version` | **server-owned** | integer | optimistic-concurrency counter; never author |
| `plan-modified` | **server-owned** | `YYYY-MM-DD` | server-stamped on each write |

**Status enum** (lenient-read; strict-write via `validate_for_write`):
`draft` · `pending` · `active` · `in-progress` · `blocked` · `shipped` · `done`
· `superseded` · `abandoned` · `archived` · `historical` · `reference`

Off-enum values on existing plans are preserved on read (lenient); they are
rejected at the write boundary.

## Plan refs — local vs external (cross-project) dependencies

Every link-list meta (`plan-depends-on`, `plan-blocks`, `plan-informs`,
`plan-evidence-for`, `plan-verifies`, `plan-supersedes`) holds **plan refs**
with one grammar:

```
ref     :=  [ project ":" ] slug [ "#" stage ]
project :=  a key in mounts.json           (e.g. nova, norma, imas-ambix)
slug    :=  a plan slug in that project
stage   :=  an optional section/stage anchor
```

- A **bare slug is a local ref** — resolved inside the owning project, exactly
  as before. Nothing changes for existing plans.
- A **`project:`-qualified ref is an external ref** into another mounted
  project (`nova:nova-spine-refactor`). A qualifier naming the owning project
  itself reads as local.
- Malformed refs (empty segments, two colons, spaces) are **rejected at the
  write boundary** (`validate_for_write`).

Resolution is the server's job, so plan files stay portable:

- `read_plan(project, slug)` (MCP) returns a computed **`deps`** list beside
  `data` — one row per `depends_on` entry:
  `{ref, scope: local|external, project, slug, found, status, impl, title}`.
  Local refs honour `checkout_path`; external refs always resolve through the
  mounts-registered MAIN checkout of the other project.
- The MCP **audit** reports external refs that do not resolve:
  `dangling-external-ref` (project mounted, slug missing) and
  `unmounted-external-project` (project absent from mounts.json) — both
  `warn`, since another machine may legitimately not mount every project.
- `doccheck` stays corpus-local by design: it validates local refs against the
  project corpus and passes qualified refs through to the server-side audit.

Dependency gating (reckon-ship) treats an unshipped external prerequisite
exactly like an unshipped local one — the `deps` rows carry the status either
way; only the resolution path differs.

## File anatomy (plan)

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="docs-project" content="<project>">   <!-- required -->
  <meta name="reckon-type" content="plan">
  <meta name="plan-slug" content="<slug>">          <!-- optional; default = filename stem -->
  <meta name="plan-title" content="Human Title">    <!-- required-on-write -->
  <meta name="plan-status" content="active">        <!-- set on lifecycle transition -->
  <meta name="plan-roi" content="high">             <!-- authored scalars -->
  <meta name="plan-effort" content="M">
  <meta name="plan-milestone" content="PS">
  <meta name="plan-sprint" content="S4">
  <meta name="plan-capability-version" content="1.0">
  <meta name="plan-capability-class" content="general">
  <meta name="plan-capability-verification" content="strict">
  <meta name="plan-summary" content="one-line synopsis">
  <meta name="plan-owner" content="Simon McIntosh">
  <meta name="plan-depends-on" content="slug-a,research-x">
  <!-- plan-version / plan-modified are server-owned — NEVER author. plan-impl is set by reckon-ship per landing (shipped/total). -->
  <title>Human Title | <project></title>
  <link rel="stylesheet" href="/_shared/foundation.css">
  <link rel="stylesheet" href="/_shared/dashboard.css">
</head>
<body>
  <main class="plan-doc">
    <!-- AUTHORED PROSE: ordinary HTML. <h2 id="s1"> so comments anchor to it. -->

    <!-- reckon-owned sections (regenerated on write; the SPA renders the
         decisions section interactively and hides the static copy). -->
    <section data-reckon="decisions" id="decisions" class="r-decisions">
      <h2><span class="sec">§</span> Decisions</h2>
      <div class="r-dec" data-key="command-name" data-choice="build" data-by="smc" data-when="2026-05-27">
        <p class="r-dec-q">CLI verb for the static-build command</p>
        <p class="r-dec-ctx">optional context</p>
        <p class="r-dec-opts">
          <button class="r-opt chosen" data-value="build">build</button>
          <button class="r-opt" data-value="export">export</button>
        </p>
        <p class="r-dec-rat">free-form rationale</p>
      </div>
      <!-- a decision with no <button> options is pure free-form: data-choice
           holds the typed answer. -->
    </section>

    <section data-reckon="followups" id="followups" class="r-followups">
      <article class="r-fu" data-id="f1" data-status="open"
               data-capability-version="1.0" data-capability-class="general"
               data-capability-verification="strict"
               data-written-by="smc" data-written-at="2026-05-27"
               data-recommends-skill="/reckon-ship slug"
               data-resolved-at="" data-resolved-by="">
        <h4 class="r-fu-title">…</h4>
        <div class="r-fu-body">…</div>
        <pre class="r-fu-prompt">§05 copy-paste prompt — MANDATORY</pre>
        <!-- when resolved: data-resolved-at/-by set + <p class="r-fu-outcome">…</p> -->
      </article>
    </section>

    <section data-reckon="questions" id="questions" class="r-questions">
      <div class="r-q" data-id="q1" data-section="§2" data-status="open"
           data-opened-by="smc" data-opened-at="2026-01-01"
           data-resolved-at="" data-resolved-by="">
        <p class="r-q-body">…</p>
        <!-- when resolved: <p class="r-q-resolution">…</p> -->
      </div>
    </section>

    <section data-reckon="research" id="research" class="r-research-list">
      <div class="r-research" data-id="r1" data-type="paper" data-source="arxiv"
           data-added-by="smc" data-when="2026-01-01" data-url="https://…">
        <span class="r-research-title"><a href="https://…">Title</a></span>
      </div>
    </section>

    <section data-reckon="comments" id="comments" class="r-comments">
      <div class="r-comment" data-section="s1" data-id="c1" data-who="smc"
           data-when="2026-05-27" data-quote="anchor text">
        <div class="r-comment-body">…</div>
      </div>
    </section>
  </main>
</body>
</html>
```

## File anatomy (research)

```html
<head>
  <meta name="docs-project" content="<project>">
  <meta name="reckon-type" content="research">
  <meta name="plan-slug" content="<slug>">
  <meta name="plan-status" content="reference">
  <meta name="plan-summary" content="one-line synopsis">
  <meta name="plan-informs" content="plan-a,plan-b">   <!-- plans this research feeds -->
  <title>Finding title | <project></title>
  <link rel="stylesheet" href="/_shared/foundation.css">
  <link rel="stylesheet" href="/_shared/dashboard.css">
</head>
<body>
  <main class="plan-doc">
    <!-- the research content as prose; no decision/followup sections -->
  </main>
</body>
```

`reckon/_plan_html.py` `read_state(html)` parses this into a dict; `write_state(html, state)`
regenerates the `data-reckon` sections + `<meta>` from the dict, leaving the
authored prose byte-for-byte intact.

### Decision data model
A decision has: `title` (the question), optional `context`, optional discrete
`choices` (option values; labels via the button text), a `choice` (the locked
answer — an option value OR free text), `rationale`, `when`, `by`. `choice == ""`
means open — select-from-options and/or a free-form answer. The "locked" state
is **derived** from a non-empty `data-choice`; no separate flag is stored.

### Followup status derivation
`status=resolved` is **derived** from `data-resolved-at` being non-empty. A
literal `data-status` attribute is kept for backward compat but the parser
overrides it if `resolved_at` is present. Never duplicate: just set
`data-resolved-at` and `data-resolved-by` to resolve.

## Distributed project state

Canonical resources are:

- `docs/sprints/<id>.html` — versioned sprint definition and ordered item
  metadata. Item lifecycle status and implementation fraction are resolved
  live from plan HTML.
- `docs/milestones/<id>.html` — one independently versioned milestone.
- `docs/blockers/<id>.html` — one blocker; reference count is derived from
  sprint item `blocked_by` references.
- `docs/state/<project>/timeline.html` — one append-only, versioned event
  stream with deterministic event ids.
- `docs/state/<project>/project.json` — identity and presentation only; no
  active-sprint pointer, collections, rollups, or host checkout path.

The active sprint id is derived from the unique sprint whose status is
`active`. Zero active sprints is valid; more than one is an audit error.
Each resource owns its optimistic-concurrency version, so unrelated writes do
not contend.

`reckon migrate-project-state docs --project <project>` is the only activation
path. It preserves a byte-identical snapshot below
`docs/.reckon/snapshots/project-state/`, stages and validates all resources,
proves composed parity, rechecks the source, installs resources, and publishes
`docs/.reckon/project-state-migration.json` last. Marker absence/staging means
legacy mode; a complete distributed marker forbids fallback.

## Legacy compatibility view — `index.json`

`read_plan(project, "index")` and `GET /state/<project>/index.json` return a
composed compatibility shape with `source_format` and `resource_versions`.
After distributed activation, aggregate writes fail with
`legacy_index_read_only`; callers edit a named resource with `doc_type`.
The retained source file has the historical schema below:

```json
{
  "updated": "YYYY-MM-DDTHH:MM:SS",
  "project": "<project>",
  "doc": "index",
  "data": {
    "_version": 7,
    "active_sprint_id": "S5",
    "sprints": [
      {
        "id": "S5",
        "theme": "Foundation hardening",
        "description": "…",
        "status": "active",
        "starts": "2026-05-26",
        "ends": "2026-06-06",
        "items": [
          {
            "slug": "my-plan",
            "title": "…",
            "roi": "high",
            "effort": "M",
            "milestone": "M2",
            "why_now": "…",
            "done_when": "…",
            "status": "pending",
            "capability": {
              "version": "1.0",
              "class": "general",
              "requirements": {"verification": "strict"}
            },
            "blocked_by": []
          }
        ],
        "summary": null
      }
    ],
    "milestones": [
      {
        "id": "M2",
        "name": "…",
        "status": "planned",
        "depends_on": ["M1"],
        "evidence": []
      }
    ],
    "timeline": [
      { "when": "YYYY-MM-DD", "who": "smc", "what": "…" }
    ],
    "blockers": [
      { "id": "b1", "summary": "…", "origin": "my-plan", "n": 2, "owner": "smc", "next": "…" }
    ]
  }
}
```

Key notes:
- `_version` was the legacy optimistic-concurrency counter. Distributed
  resources have independent `version` values.
- `inventory[]` is **synthesised live** by `discover_plans` on every `GET /_discover/<project>` call. It is **never persisted** to `index.json` (`exclude=True` in `IndexData`). Sprint items reference plan slugs; current plan state (impl, decisions, followups) is always read from the plan's HTML.

## Lenient read / strict write

`reckon/_plan_html.py` `from_html` is **lenient** — it coerces/normalises and
never raises. Off-enum values, missing fields, and old plans all parse cleanly.
Validation of enum membership and required-on-write fields runs **only** at the
explicit write boundary, via `PlanState.validate_for_write()` (wired into
`edit_plan` by the store layer). This means:

- Reading any old plan will succeed.
- Legacy `plan-tier`, `data-tier`, and sprint-item `tier` values map
  deterministically on read and emit a compatibility audit warning. They remain
  unchanged until an explicit capability migration or normal capability edit.
- Writing a plan with `status="foo"` or empty `project` field is rejected.
- A followup without a non-empty `prompt` is rejected at write time.

## `edit_plan` write contract

`edit_plan(project, slug, ops, expected_version, create=False, doc_type=None)`
is the single safe write path. `doc_type` selects plan/research/evidence or a
named sprint/milestone/blocker/timeline/project resource. Ops are applied in
order, schema-validated, then atomically
version-checked and written. **Optimistic concurrency:** call `read_plan`
first to get `version`; pass it as `expected_version`; on 412 re-read and retry.

**Verb reference:**

| Verb | Shape | Replaces |
|---|---|---|
| `set` | `{"op":"set","path":"<scalar-field>","value":…}` | setting any meta scalar |
| `append` | `{"op":"append","target":"<section>","item":{…}}` | adding to a list section |
| `resolve` | `{"op":"resolve","target":"followups\|questions","id":"…","by":"…","outcome"\|"resolution":"…"}` | resolving a followup or question |
| `lock` | `{"op":"lock","key":"<dec-key>","choice":"…","rationale":"…","by":"…"}` | locking a decision |
| `move` | `{"op":"move","target":"sprint_item","slug":"…","to":"S2","to_version":4}` | moving an item from the selected sprint with both versions checked |

**`set` path values** (plan):
`status` · `impl` · `roi` · `effort` · `milestone` · `sprint` · `capability` · `owner`
· `summary` · `title` · `type` · `archived` · `read` · `depends_on` · `blocks`
· `informs`

**`set` path values** (distributed resource):
top-level mutable fields on the selected sprint, milestone, blocker, or
project resource. Timeline state is append-only.

**`append` target values** (plan):
`followups` · `research` · `questions` · `comments` · `decisions` (with `key`)

**`append` target values** (distributed resource):
`items` on a sprint; `events` on the timeline.

**Create a new plan:**
`edit_plan(project, slug, ops=[…], expected_version=0, create=True)`

**Discovery / read:**
- `read_plan(project, slug, doc_type=...)` — one exact typed resource + version
- `read_plan(resource={project, type, id})` — concise typed summary by default
- `view=detail|history|raw|schema` — progressively reveal only the needed layer
- `read_plan(project, "index")` — composed compatibility view
- `read_plan(project, slug, with_schema=True)` — injects schema + dos/don'ts inline
- `read_plan(project, slug=None)` — discovery: inventory + followups/questions/sprints facets

Typed write workflows should read the selected resource with `view="raw"` to
obtain its current lossless state and version. Status and review workflows
should begin with `summary`; `history`, discovery detail, and audit detail are
cursor-paginated. `audit(project, view="summary")` returns compact counts,
`view="detail"` returns findings, and `view="raw"` preserves the legacy audit
payload.

Before distributed activation, typed sprint/milestone/blocker/timeline/project
reads project one named record from the canonical legacy index and carry its
aggregate version plus a compatibility warning. Named writes remain inactive:
write the legacy index until explicit migration publishes the distributed
marker. After activation, reads and writes use only independently versioned
named resources.

## Server endpoints

| Method · path | Purpose |
|---|---|
| `GET /<project>` and `/<project>/` | the SPA shell |
| `GET /<project>/<type-root>/<slug>` | canonical typed doc route |
| `GET /<project>/<slug>.html` | bounded flat compatibility redirect |
| `GET /_discover/<project>` | inventory (each entry carries `type` + parsed state) + sprints + milestones |
| `GET /state/<project>/index.json` | composed read-only compatibility view |
| `GET /plan/<project>/<type-root>/<slug>` | typed parsed state (incl. `version`) |
| `GET /plan/<project>/<slug>` | compatibility read; an actionable plan wins an untyped collision |
| `GET /_shared/plan.schema.json` | the published JSON Schema (derived from `_schema.py:PlanState`) |
| `POST /plan/<project>/<slug>` | merge a flat dotted patch into the state and rewrite the HTML elements. `If-Match: <version>`; 412 returns `{current_version, current_data}` — prefer `edit_plan` MCP tool over raw POST |

Patches are dotted, e.g. `decisions.command-name.choice` → sets `data-choice`
on that `.r-dec` and marks the matching option `.chosen`, preserving the
question/options. Scalars (`status`, `impl`, …) update the `<meta>`. All SPA
URLs are absolute; `reckon build` rewrites them to relative for static hosting.
