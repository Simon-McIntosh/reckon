---
name: reckon-review
description: >-
  Review between nodes — one code neighbourhood, one plan, or a portfolio —
  starting from the findings already stored and proposing at most one treatment
  that deletes or narrows rather than adds. Code mode composes a read-only
  review around a plan or a path, reads the stored findings, proves the
  treatment against the code, and dispatches it or records why not. Plan mode
  audits one plan's currency. Portfolio mode proposes close, merge, retire and
  re-sprint dispositions. Trigger verbs: "review the code / review this plan /
  review the portfolio / review the sprint / health review / /reckon-review".
  Read the plan first; apply dispositions with reckon-edit and reckon-sprint and
  execute a treatment with reckon-build.
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

## Resolve the mode from the target

| Target | Mode | Loop |
|---|---|---|
| `--around <plan>` or `--path <path>` | code | §Code |
| `--plan <slug> --rubric currency` | plan | §Plan |
| `--sprint <id>` or `--portfolio` | portfolio | §Portfolio |

`--plan <slug> --rubric content\|design` composes an ordinary content or design
review; that is the pre-build gate `reckon-build` and `reckon-create` already
own, not this skill's mode.

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

   Read the rows back with `reckon crew ledger --project <project> --view findings --path <prefix>`.

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
the roadmap's ready-or-blocked verdict.

## Portfolio

One `DISPOSITION <slug>: keep | close | merge-into <slug> | retire | re-sprint
<id> — <evidence>` line per plan in scope, from the same review verb:

```bash
reckon crew review --project <project> --sprint <id> --session <session>
reckon crew review --project <project> --portfolio --session <session>
```

Apply the dispositions: `close`, `merge-into` and `re-sprint` through
`reckon-edit` and `reckon-sprint`; archive a retirement with `plan-archived=1`.
Record the round in the sprint review resource (`docs/state/reckon/review.html`).
**A merge or retirement of a plan another live session is building is proposed,
never applied** — check `crew(project, view="live")` before writing.

## The stop rule

After ten code-mode reviews, if fewer than half of the treatments were promoted,
or the promoted treatments added product lines in total, the build skill's
review step becomes opt-in and the lead decides whether to keep it. Read the
yield from `crew(project, view="velocity", fields=["health_reviews"])` before
composing the next review, and state the current count when you recommend
stopping.

## Refusals

A finding list that is empty is not a clean review unless the brief actually
carried the neighbourhood — check the composed brief names paths before
reporting no findings. Never dispose a finding you did not read. Never widen
this skill's own write scope: it composes, reads, prototypes and disposes; the
plan's product source is written by a dispatched node, and plan state by
`reckon-edit`