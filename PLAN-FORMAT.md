# reckon plan format

> Single source of truth for the format. The server (`reckon/serve.py`,
> `reckon/_plan_html.py`), the SPA (`docs/ui/*.jsx`), the MCP layer
> (`reckon/_store.py`, `reckon/mcp.py`), and the skills (`skills/reckon-*`)
> all assume exactly this. One format.

## Core principle

**The data IS semantic HTML.** Everything lives inside the `.html` file as
ordinary HTML the reader can see — decisions, followups, questions, research,
comments — plus a few `<meta name="plan-*">` scalars. There is no embedded
data blob and no per-plan state sidecar. The reckon server reads these elements
for discovery and rewrites them in place on a live edit.

A project keeps one `docs/state/<project>/index.json` for project-level config
only — sprints, milestones, `active_sprint_id`, timeline.

## Any HTML file is a doc

Existence is sufficient: any `*.html` under a project's docs dir — except the
infrastructure files/dirs below — is surfaced. Markup only enriches it; a bare
page surfaces with `status=draft` and its `<title>` as the title.

- Excluded dirs: `_shared`, `ui`, `state`, `assets`, `images`, `archive`.
- Excluded files: `index.html`, `sprints.html`, `milestones.html`,
  `decisions.html`, `inventory.html`, `blockers.html`, `questions.html`,
  `home.html`, `project.html`.
- Per-stage history (`<plan>-shipped.html`, `*-locked.html`, …) lives under
  `archive/`.

## Two document types

`<meta name="reckon-type" content="plan|research">` (default `plan`).

- **plan** — an actionable unit with decisions, followups, status, impl.
- **research** — a non-actionable *input*: a reference/finding/analysis that
  one or more plans build on. It carries prose + `<meta name="plan-informs"
  content="slug-a,slug-b">` listing the plans it feeds. It has no decision /
  followup workflow. Plans reference research via `plan-depends-on`, so the
  link is traversable both ways (a plan shows its research inputs; a research
  doc shows the plans it informs).

## File anatomy (plan)

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="docs-project" content="<project>">   <!-- required -->
  <meta name="reckon-type" content="plan">
  <meta name="plan-slug" content="<slug>">          <!-- optional; default = filename stem -->
  <meta name="plan-status" content="active">        <!-- server-written -->
  <meta name="plan-impl" content="0.6">             <!-- server-written -->
  <meta name="plan-version" content="3">            <!-- server-owned concurrency counter -->
  <meta name="plan-roi" content="high">             <!-- authored scalars -->
  <meta name="plan-effort" content="M">
  <meta name="plan-milestone" content="PS">
  <meta name="plan-sprint" content="S4">
  <meta name="plan-tier" content="sonnet">
  <meta name="plan-summary" content="one-line synopsis">
  <meta name="plan-depends-on" content="slug-a,research-x">
  <title>Human title | <project></title>
  <link rel="stylesheet" href="/_shared/foundation.css">
  <link rel="stylesheet" href="/_shared/dashboard.css">
</head>
<body>
  <main class="plan-doc">
    <!-- AUTHORED PROSE: ordinary HTML. <h2 id="s1"> … so comments anchor to it. -->

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
      <article class="r-fu" data-id="f1" data-status="open" data-tier="sonnet"
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
means open — select-from-options and/or a free-form answer.

## Server endpoints

| Method · path | Purpose |
|---|---|
| `GET /<project>` and `/<project>/` | the SPA shell |
| `GET /<project>/<slug>.html` | the doc page (SPA fetches this for prose) |
| `GET /_discover/<project>` | inventory (each entry carries `type` + parsed state) + sprints + milestones |
| `GET /state/<project>/index.json` | project config + live-merged inventory |
| `GET /plan/<project>/<slug>` | the parsed doc state (incl. `version`) |
| `POST /plan/<project>/<slug>` | merge a flat dotted patch into the state and rewrite the HTML elements. `If-Match: <version>`; 412 returns `{current_version, current_data}` |

Patches are dotted, e.g. `decisions.command-name.choice` → sets `data-choice`
on that `.r-dec` and marks the matching option `.chosen`, preserving the
question/options. Scalars (`status`, `impl`, …) update the `<meta>`. All SPA
URLs are absolute; `reckon build` rewrites them to relative for static hosting.
