# Worker verification — reading what a worker produced

Read this reference when a worker has landed and you are about to read what it
produced; the one-line mandate stays in `../SKILL.md` §5. It is why the gate a
worker writes for itself cannot stand alone, what the cheap checks are in
yield-per-second order, how to read the diff by anomaly and set depth, why the
coordinator names the measure itself, and when to batch an independent review.

### 5b. Read what the worker produced — the gate is not the evidence

**Authored from measurement, not preference.** Two coordinators running fleets on
2026-09-05 reported their converged practice over twenty-four landings between
them. Both had independently stopped treating a passing gate as the verification
and started treating it as one input to a read. This section is that practice.

**The distinction that aims everything below, and the single most useful sentence
either of them wrote:**

> A manifest reports what the worker knows it did, including what it knows went
> wrong. It cannot report what the worker believes is correct and isn't.

So self-reporting is *reliable* for fence violations, vacuous measures, mis-aimed
briefs, and declared deviations — workers report those well, and today's corpus
shows them doing it unprompted. **Reading is the only thing that catches
confidently-wrong work.** Aim the read at that, and it stops being a ritual.

**Why the gate cannot stand alone: it is usually the worker's own instrument.**
When the implementing worker writes the check and the code, a pass measures
internal consistency, not correctness — the check encodes the same
misunderstanding as the code. Four measured shapes, all with green gates:

| Shape | Instance |
|---|---|
| Gate written against its own diff, inputs never leaving the preserved regime | a census compaction whose byte-identity gate used fixtures where the truncation was unreachable |
| Instrument agreeing with the only fixture it was pointed at | speculative counters reading zero against an engine publishing them, because the fixture was written by the parser's own author |
| Gate ran but never finished | an after-gate log stopping mid-run because the worker backgrounded it and its process ended; the ledger says passed |
| Gate unevaluable on real data | a measure asking for a condition the data never satisfies, carried on a fixture built to satisfy it |

#### The cheap checks, in yield-per-second order

Run these before reading any code. Each is one command, and each catches a class
the later ones cannot.

1. **Manifest mtime against dispatch time.** Catches the worst failure — a
   manifest describing a *previous attempt* — which no content check can catch,
   because the content is internally consistent. Staleness is its own axis.
2. **`commits:` against `git -C <worktree> rev-parse HEAD`.** Mismatch was
   observed in roughly a third of one backend's manifests in a single week.
3. **`git -C <worktree> status --porcelain`.** Dirty means the commit list is
   incomplete, whatever the manifest says.
4. **`git show --stat <sha>` against the declared write paths.** Scope drift,
   instantly.
5. **Gate log: does the file exist at the claimed path, does its header name the
   same tests as the `tests:` line, does its last line agree with the claim.**
6. **Manifest status word against process liveness.** `in_progress` with a dead
   process is the ended-with-its-turn case, and no terminal event ever fires for
   it.
7. **Declared deviations — last.** They tell you the worker is honest, not that
   the work is right.

#### Read the diff by anomaly, not sequentially

**The highest-yield technique reported, and it inverts the obvious approach.** On
a change that applied one rule to 118 sites, filtering the diff for lines matching
the change's signature but **not** its expected shape returned five hits — and
three of the four real defects were among those five. A sequential read finds
those at hunk 90 of 118, or never.

So: derive the change's expected shape, filter for what does not match it, and
read the residue. Fall back to a full hunk read where the pattern is
non-uniform, or where the file carries blast radius.

#### Setting depth — and the two axes that do NOT set it

Depth follows **blast radius × mechanicalness**:

| Change | Depth | Measured yield |
|---|---|---|
| Scripted change applied at scale across a large module | full anomaly scan | four defects |
| Small hand edit to one function | skim | none |
| A shared enforcement predicate | read the predicate in full, nothing else | — |
| Pure refactor with bit-identity over many files | manifest and stat only | none, repeatedly |

Read test diffs for **inputs and assertions** — above all, whether fixtures are
synthetic — not line by line. Read the stream only when something disagrees, or
to find out why a process ended: a successful node's stream runs to tens or
hundreds of thousands of records and is nearly all noise.

**Role and spec level do not set depth.** Neither does the backend. Lane did not
predict manifest honesty in the measured corpus — locally-served and frontier
workers self-attributed their own failures at the same rate — and a coordinator
who reads shallowly because a frontier model produced the diff is reading the
wrong variable. What predicts defects is **mechanical-at-scale**: a script that
gets one rule slightly wrong applies it uniformly, and no worker reviews its own
scripted output line by line.

**Expect roughly half of reads to return nothing material**, and treat that as the
cost of the reflex rather than a sign it is miscalibrated. Measured: of twelve
landings, line reads changed the outcome on two, refined the record on three, and
confirmed seven; the other coordinator put material returns at 40–50%. A read that
finds no defect but returns a habit worth copying has still paid.

#### Name the measure yourself — as an enabler, not a control

**The coordinator names the done-when measure numerically, and this is the rule
with the best evidence behind it.** A coordinator-supplied numeric measure is the
only instrument a worker has for *contradicting the coordinator*. Two measured
instances: a worker held to a stated numeric contract over the coordinator's
prose, proved two of the coordinator's four findings misattributed, and supplied a
fifth defect in their place; another found the coordinator's two-sided gate could
not discriminate and identified a cause in a function the brief never named. Both
corrections travelled *through* the supplied measure.

A worker writing its own gate writes one that agrees with its own reading of the
brief, and loses that capability. State the rule that way round: "name the measure
so the worker can prove you wrong" is followed; "we do not trust worker gates" is
worked around.

#### Independent review — a reflex with a trigger, not a universal

Batch one read-only review node per wave over that wave's merged diffs, **when the
trigger fires**: a merged production diff whose gate is an identity comparison or
whose fixtures are synthetic, touching a shared authority, integrated without a
line read. Record the reviewer's provenance beside the coordinator's on each row.

Measured cost and return: **$6.51 and 14 minutes for three commits totalling about
1,100 changed lines, returning one HIGH, two LOW and two coverage holes** —
against $12–23 per implement node. It found what an audit of thirty merges had
not. It would have found nothing on the pure refactor above, which is exactly why
the trigger matters.
