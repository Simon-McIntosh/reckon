# Design canvas imports

Source-of-truth design specs imported from the Claude Design canvas project
`Reckon SPA design review` (`claude.ai/design/p/4d84ed98-9413-4d4e-8eb1-1a641b338b24`).

| File | What it is |
| --- | --- |
| `reckon-spa-prototype.dc.html` | Interactive prototype of the whole served surface: topbar + project chips + Manage, Plans (chip filters, list, reader, evidence rail), Overview, command palette, reading mode, Sprints (gantt overview + board), Crew, Graph. |
| `reckon-spa-redesign.dc.html` | Annotated redesign. Seven named surfaces, each quoting the lead's verbatim complaint about the shipped SPA beside the surface as it should read. |

Both are `.dc.html` canvas documents: an `<x-dc>` template using `sc-if` /
`sc-for` / `{{ binding }}` and a `<script data-dc-script>` holding a `DCLogic`
subclass whose `state`, fixture `D` and `renderVals()` supply every binding.
The canvas runtime that interprets them (`support.js`) is generated and is not
vendored here — read these files as specification, not as runnable code.

The fixture mirrors real state read from `Simon-McIntosh/{reckon,nova,imas-codex}@main`
on 2026-08-24, so the shapes it binds are the shapes the server already serves.
Reading order for an implementer: the annotated redesign first for *why*, then
the prototype for *what the surface should be*.

Re-import with the `DesignSync` tool (`get_file`) against the project id above;
these copies are point-in-time and are not synced automatically.
