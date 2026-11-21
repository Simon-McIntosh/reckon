---
name: reckon-select
description: >-
  Present every decision waiting on the lead as one set of choices, in any one
  session, and record each answer. Use when the lead asks what needs deciding,
  asks for the decisions to be put as choices, or says answer the open
  decisions. Trigger phrases: "what needs deciding", "what's waiting on me",
  "put the decisions to me", "present the decisions", "answer the decisions",
  "/reckon-select".
allowed-tools: Bash(*) Read
---

# reckon-select — one session puts the decisions to the lead

Work that waits on the lead's word is held work: queued requests, runs,
deferred sections. This skill is the one place the lead answers it. It reads
the fleet's open decisions, asks the live sessions for any decision they hold
without a record, presents the choices largest held work first, and records
each answer through the presenting path. A record carries the question, its
context, two to four options with the owner's recommendation first, the owning
repository, session, plan and section, and what the answer releases.

The answer is the record. Nothing is messaged on to its owner: the owning
session reads the answer from the obligations view at its next turn, and its
held requests leave their hold at the next admission pass.

## Never

- **Never answer a decision from the session that owns it.** Only the
  presenting session may answer what it presents; the store refuses any other,
  and that refusal is the guarantee the lead's hold cannot be released by the
  work it holds.
- **Never present by re-reading a decision you did not claim: the claim is the
  order.** `present` claims each choice for this session before returning it.
- **Never invent a decision.** A decision a session has not recorded is asked
  for in step 2, not written in its name.
- **Never hold the lead in the terminal's own question dialog for a batch.**
  The choices are printed as one set, and the answers are recorded one call at
  a time.

## 1. Ask the live sessions once

```bash
reckon crew decision present --session <this-session> --pretty
```

The call's `asked` list is the asks it wrote this time — one per live session
that can be reached — and `already_asked` is what an earlier presentation
already asked. The ask record names the peer and the one-line message. Send
each with the harness's own peer message to that `name`, once:

> If a decision waits on the lead and is not recorded, record it with
> `reckon crew decision record --record -` so it can be presented.

`unreached` lists a live session the ask cannot reach, with the reason: no
harness record names a live process for it, or the record names no peer. Name
each of those in the report rather than passing over it. `replied` lists the
sessions that have since recorded a decision.

Ask once. A second send to the same session is the follow-up this skill does
not do: the ask is a record a busy session finds when it next looks.

## 2. Present the choices

The same call's `choices` are the set, in the order the lead answers them:
`ready` decisions largest held work first, then the decisions written only in a
plan's decisions block, which hold nothing back and are answered by locking
them in their plan. Show each choice with:

- the owning repository and session,
- the plan and section,
- the context, as recorded — do not summarise it,
- the options in the order returned, which is recommendation-first, marking
  which one the owner or reviewer recommended and by whom,
- what the answer releases: the requests, runs and sections it holds, or the
  risk when it holds nothing.

A choice carrying `absent` names the fields it does not have — a plan-block
decision has no owning session, no recorded time and no held work. Say so for
each; never read those as empty or as zero. `released: null` is the same fact
for the release payload.

`claimed_elsewhere` — decisions held by another presenting session — are named
with the session holding them. Do not present those: the session holding the
claim is showing them to the lead. `unread_projects` names a project whose documents
could not be read, with the refusal's words — report it, so the lead knows a
project contributed nothing rather than nothing being held.

Read the answers back to the lead as they are given. One batch, no re-asking.

## 3. Record each answer

```bash
reckon crew decision choose --id <decision> --option <value> \
  --session <this-session> [--note "<the lead's words>"]
```

The call stores the chosen option, the note and the time, closes the decision,
and names the choice. A plan-block decision is answered the same way and lands
as a lock in its plan.

After each answer, report the choice beside the owner's recommendation. Where
the lead chose differently, say so: the recommendation was not followed, and
the owning session will read the answer next to it.

## 4. Release what was not answered

```bash
reckon crew decision release --session <this-session>
```

Give up the claims on the choices the lead did not reach, so the next
presentation — in this session or another — offers them again. A claim left
behind hides the decision from every other presenter, which reads as a decision
that no longer waits.

## 5. The report

One report, in the order presented:

- each decision, its repository, session, plan and section, and its held work;
- the option the lead chose, marked where it differs from the recommendation;
- what left its hold at the next admission pass;
- the live sessions that replied, and each session named in `unreached` with
  the reason;
- any `unread_projects` entry, with the refusal.

The report is the whole record of the presentation. Nothing is messaged to an
owner afterwards; the answer is read from the obligations view.