---
name: reckon-review
description: >-
  Review between nodes — one code neighbourhood, one plan, or a portfolio —
  starting from the findings already stored and proposing at most one treatment
  that deletes or narrows rather than adds. Code mode composes a read-only
  review around a plan or a path, reads the stored findings, proves the
  treatment against the code, and dispatches it or records why not. Plan mode
  audits one plan's currency and judges every one of its links. Portfolio mode
  proposes close, merge, retire and re-sprint dispositions and prunes the links
  between the plans in scope as readily as it adds them. Trigger verbs: "review
  the code / review this plan / review the portfolio / review the sprint / health
  review / check the plan links / /reckon-review". Read the plan first; apply
  dispositions and link changes with reckon-edit and reckon-sprint and execute a
  treatment with reckon-build.
allowed-tools: Read Write Edit Bash(*) Grep mcp__reckon__read_plan mcp__reckon__edit_plan mcp__reckon__roadmap mcp__reckon__audit mcp__reckon__crew
---

# reckon-review — the review between nodes

## Fan-out boundary

In a crew-managed repository, investigation and review fan-out is `reckon crew dispatch` work under the investigate and review roles; harness-native background agents bypass the run ledger, manifests and calibration and are refused by the guard.

## The busy-work rule

A review that generates findings from nothing is busy work, because the yield of
review is low where the subject is small. This one starts from what is already
known: the findings stored against the paths in scope, ranked by recurrence. It
proposes **at most one treatment**, and a treatment qualifies only if it deletes
code, narrows an interface, wires or deletes unreached code, or closes a finding
two or more reviews have already raised. If nothing qualifies, say
`TREATMENT: none — <reason>`; that is a complete review, not a failure.

The same rule governs links: a link review that only adds edges is not a review.
Every existing link in scope gets a verdict, and removing a link that no longer
orders anything is as much a result as adding one that was missing (§Links).

## Resolve the mode from the target

| Target | Mode | Loop |
|---|---|---|
| `--around <plan>` or `--path <path>` | code | §Code |
| `--plan <slug> --rubric currency` | plan | §Plan, then §Links |
| `--sprint <id>` or `--portfolio` | portfolio | §Portfolio, then §Links |

`--plan <slug> --rubric content\|design` composes an ordinary content or design
review; that is the pre-build gate `reckon-build` and `reckon-create` already
own, not this skill's mode.

## Tools

Composing a review is a CLI verb (`reckon crew review`); everything after it has
an MCP form. Use the MCP form first, and say in the report when you fell back.

| Need | Call |
|---|---|
| Open work, readiness, critical path, wiring findings | `roadmap(project)`; narrow with `sprint=<id>`; page with `limit` and `cursor` |
| One plan's links, sections, gates and followups | `read_plan(project, slug, view="summary")`; one section with `view="section", section="sN"` |
| Document and project audit (unwired, stale, missing impl) | `audit(project)`; one file with `audit(path=<file>)` |
| Stored plan reviews and their unanswered findings | `crew(project, view="plan-review", plan=<slug>)` |
| Stored code findings by path | `crew(project, view="findings", path=<prefix>)` |
| Who is working on a plan now, and which paths they hold | `crew(project, view="live")`, `crew(project, view="scopes")`, `crew(view="directory")` |
| Review yield for the stop rule | `crew(project, view="velocity", fields=["health_reviews"], since=<date>)` |
| Answer a plan-review finding | `crew(action="answer", project, plan, finding, acted=true)` or `declined="<reason>"` |
| Dispose a code finding | `crew(action="dispose", project, run_id, finding_index, kind, node \| reason)` |
| Change a link, a gate or a plan's status | `edit_plan` through `reckon-edit` (ops in §Links) |

When `roadmap(view="detail")` is refused as over budget, narrow it by sprint or page
it before reaching for `reckon roadmap --json-output`, and name the fallback in the
report: a fallback the report hides is a defect nobody fixes.

## Code

1. Compose the review and store it. It resolves the neighbourhood — the named
   paths, or the plan's implementable write paths, plus every production module
   one import away — and dispatches one read-only review node.

   ```bash
   reckon crew review --project <project> --around <plan> --session <session>
   reckon crew review --project <project> --path <path> --path <path> --session <session>
   ```

   Add `--dry-run` to read the composed brief before it runs. Read the stored
   result with `crew(project, view="findings", path=<prefix>)`.

2. Read the findings. Recurrence is the number of distinct reviews naming a
   path. A finding the brief already carried is known, never re-reported as new.

3. Prove the treatment against the code before proposing it. A change that
   cannot be shown to delete, narrow, wire or close is not a treatment; record
   the prototype's evidence either way.

4. Take the prototype to a node: dispatch it through `reckon-build`, with write
   paths disjoint from every live claim (`crew(project, view="scopes")`), or
   record why it is not dispatched. At most one treatment per review.

5. Dispose every finding you read; none is left `open`. A `folded` names the
   node it went into, an `exempted` names why it is not acted on:

   ```bash
   reckon crew dispose --project <project> --run <run-id> --finding <n> --kind folded --node <node>
   reckon crew dispose --project <project> --run <run-id> --finding <n> --kind exempted --reason "<why>"
   ```

   Read the rows back with `reckon crew view findings --project <project> --path <prefix>`.

## Plan

Audit one plan's currency against the code and its overlap with other live
plans. Compose the single-plan disposition pass:

```bash
reckon crew review --project <project> --plan <slug> --rubric currency --session <session>
```

Read the plan's existing content and design reviews beside it
(`crew(project, view="plan-review", plan=<slug>)`) so a settled finding is not
raised twice. Spot-check each remaining implementable section against the tree —
is it already in the code? — and apply what the review proposes through
`reckon-edit`. Report the plan's status, stored and derived impl, sprint
membership, days since its last commit, the open declarations and followups, and
the roadmap's ready-or-blocked verdict. Then judge every link of the plan (§Links).

A plan with no implementable section left is finished: close it with `close`, or
record why it stays open. A finished plan that reads `blocked` is held by a gate
or a deferred section, not by work. Name which one, and clear it through §Links,
by closing the gate or descoping or moving the section, rather than leaving the
plan open at 100 %.

A deferred section is open work: its plan is not finished, and `close` refuses
it. Move it to a successor plan with its prose, open gates and waits, naming
where it came from, or remove it citing a recorded decision that the work would
not change a result. Narrow, carry or re-anchor every gate and comment on a
section that leaves. Until `a-finished-plan-closes` §4 gives `close` a
`moved_to` descope, the move is a direct HTML edit; announce it as a bypass.

## Portfolio

One `DISPOSITION <slug>: keep | close | merge-into <slug> | retire | re-sprint
<id> — <evidence>` line per plan in scope, from the same review verb:

```bash
reckon crew review --project <project> --sprint <id> --session <session>
reckon crew review --project <project> --portfolio --session <session>
```

Apply the dispositions: `close`, `merge-into` and `re-sprint` through
`reckon-edit` and `reckon-sprint`; archive a retirement with `plan-archived=1`.
Record the round in the sprint review resource (`docs/state/<project>/review.html`).
**A merge or retirement of a plan another live session is building is proposed,
never applied** — check `crew(project, view="live")` before writing. Then judge
every link between the plans in scope (§Links). A merge or retirement moves or
removes the links that pointed at the plan in the same pass; a link left
pointing at a closed or retired plan is the stale edge the next review finds.

## Links

Links are what the roadmap computes the critical path, the ready set and blocking
from, so a wrong link is a wrong answer to "what runs next". Stale links pile up
because nothing removes them: a dependency on a plan that shipped months ago
orders nothing and still reads as structure.

Inventory every link in scope from `read_plan` and the plan's `roadmap` row:
plan relationships, each section's waits, gates and cross-project refs. Give each
one a verdict (keep, remove, narrow, convert or re-point) from the table in
[references/links.md](references/links.md), which also holds the exact
`edit_plan` ops. Add a link only on evidence that something waits on or consumes
the target. Report one line per changed link and a tally:

```
LINK <slug>[#sN]: remove|add|narrow|convert|re-point <kind> <ref> — <evidence>
LINKS: <judged> judged · <kept> kept · <removed> removed · <added> added · <changed> narrowed, converted or re-pointed
```

A tally with nothing judged, or with additions while no link was examined for
removal, is an incomplete review. Apply through `reckon-edit`. Links on another
project's plan, or on a plan a live session is building, are proposed to its
owner (`crew(view="directory")`), not applied.

## The stop rule

After ten code-mode reviews, if fewer than half of the treatments were promoted,
or the promoted treatments added product lines in total, the build skill's
review step becomes opt-in and the lead decides whether to keep it. Read the
yield from `crew(project, view="velocity", fields=["health_reviews"], since=<date>)` before
composing the next review, and state the current count when you recommend
stopping.

## Refusals

A finding list that is empty is not a clean review unless the brief actually
carried the neighbourhood — check the composed brief names paths before
reporting no findings. Never dispose a finding you did not read. Never widen
this skill's own write scope: it composes, reads, prototypes and disposes; the
plan's product source is written by a dispatched node, and plan state by
`reckon-edit`
