# Portfolio review rubric

You are reading a project's live plans — every plan whose status is not shipped
— and proposing one disposition for each. The brief beneath this rubric lists
one figure line per plan in scope: its status, its derived implementation
fraction and how that fraction was derived, its sprint, the age of its last
commit, the count of its open section declarations and followups, and the
roadmap's ready-or-blocked verdict.

Your job is the judgement the figures alone cannot make: is this plan still
worth carrying, or should it be closed, merged into another, retired, or moved
to a different sprint?

## What to read

1. **Each plan's remaining implementable sections.** For every plan in scope,
   spot-check one implementable section against the tree it writes: is the work
   it describes already present in the code? A plan whose remaining sections are
   already in the code is a `close` candidate.
2. **Clustering by subject.** Two or more plans that describe one body of work
   are a `merge-into` candidate, with the plan that should absorb the others
   named as the target.
3. **Currency.** A plan untouched for a long time whose work is still wanted is
   a `keep` or a `re-sprint`; a plan whose work is no longer wanted is a
   `retire`.
4. **Sprint membership.** A plan that belongs to no sprint, or to a closed one,
   may be a `re-sprint` candidate naming the sprint it should join.

Read the code, not only the plan prose: the figures tell you what shape a plan
is in, and the spot-check tells you whether the shape is honest.

## What to emit

Emit exactly one line per plan in scope, and no other `DISPOSITION` line:

    DISPOSITION <slug>: keep | close | merge-into <slug> | retire | re-sprint <id> — <evidence>

- `<slug>` is the plan's slug, exactly as the brief names it.
- The verb is one of `keep`, `close`, `merge-into`, `retire`, `re-sprint`.
- `merge-into` names the plan slug that should absorb this one;
  `re-sprint` names the sprint id it should join. The other verbs take no
  target.
- `<evidence>` is one sentence citing what you read — a file, a section, a
  figure from the brief — that justifies the disposition. An em dash separates
  the verb from the evidence.

A plan in scope with no `DISPOSITION` line is a refusal: the output is read as
incomplete and the missing plan is named. Every plan the brief lists must carry
one line.

## The dispositions

- `keep` — the plan is current and should stay as it is.
- `close` — the plan's remaining work is already in the code, or it is done.
- `merge-into <slug>` — the plan's work belongs to another plan, which should
  absorb it.
- `retire` — the plan's work is no longer wanted and should be archived.
- `re-sprint <id>` — the plan's work is still wanted but belongs in a different
  sprint.