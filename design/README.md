# Design canvas imports

Source-of-truth design specs imported from the Claude Design canvas project
`Reckon SPA design review` (`claude.ai/design/p/4d84ed98-9413-4d4e-8eb1-1a641b338b24`).

| File | Source file in the design project | What it is |
| --- | --- | --- |
| `reckon-spa-prototype.dc.html` | `Reckon SPA Prototype.dc.html` | Interactive prototype of the whole served surface: topbar + project chips + Manage, Plans (chip filters, list, reader, evidence rail), Overview, command palette, reading mode, Sprints (gantt overview + board), Crew, Graph. |
| `reckon-spa-redesign.dc.html` | `Reckon SPA Redesign.dc.html` | Annotated redesign. Seven named surfaces, each quoting the lead's verbatim complaint about the shipped SPA beside the surface as it should read. |
| `reckon-spa-handoff.dc.html` | `Reckon SPA v2.dc.html` | The surface as it is handed off for implementation: fleet home, search-left topbar with two tab groups, working visibility sheet, four artifact tabs (Plans / Research / Evidence / Figures) with created and edited feeds and live arrival, a dependency-derived sprint flow, a sprint detail DAG that draws out-of-sprint prerequisites, a Graph tab that renders unnamed endpoints, and a repo-scoped scrolling Crew. |
| `reckon-spa-handoff.md` | `handoff.md` | The designer's triage of the landed SPA against the handoff canvas: seven implementation defects with root causes named in source, nine design changes, the visual tokens, the open design questions, and the served-data contracts the landing depends on. |
| `canvas-layout-spec.md` | — | Layout geometry extracted mechanically from the prototype canvas. |

All `.dc.html` files are canvas documents: an `<x-dc>` template using `sc-if` /
`sc-for` / `{{ binding }}` and a `<script data-dc-script>` holding a `DCLogic`
subclass whose `state`, fixture `D` and `renderVals()` supply every binding.
The canvas runtime that interprets them (`support.js`, generated from
`dc-runtime/src/*.ts`) is not vendored here — read these files as
specification, not as runnable code. The handoff canvas also carries two
algorithms an implementer copies rather than paraphrases: `schedule()` (the
dependency-derived flow with best-fit lane packing) and `layout()` (the shared
DAG layout with routed skip-level edges and fanned arrivals).

The fixtures mirror real state read from `Simon-McIntosh/{reckon,nova,imas-codex}@main`
— the prototype on 2026-08-24, the handoff canvas on 2026-09-03 — so the shapes
they bind are the shapes the server already serves. Reading order for an
implementer: `reckon-spa-handoff.md` first for *what is wrong and why*, then
`reckon-spa-handoff.dc.html` for *what the surface should be*, then the two
earlier canvases only for history.

Re-import with the `DesignSync` tool (`get_file`) against the project id above;
these copies are point-in-time and are not synced automatically.
