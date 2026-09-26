# Worker anatomy: where turns and context go

Census of 2,326 crew runs dispatched in the study window
(`2026-09-12T00:00:00Z` .. `2026-09-26T10:00:00Z`, filtered on
`dispatched_at`); 2,317 had a parseable stream, 5 had no stream file, 4 were
unreadable. Every figure below carries its denominator; the numbers are
`anatomy.json` (`median`/`p90`, sample `n` per metric cell).

Population: `promoted_passed` 1,579, `promoted_not_run` 487, `failed` 115,
`complete_unpromoted` 114, `no_terminal_record` 19, `blocked` 3. A
`discarded` stratum resolves to zero: `crew discard` leaves no durable per-run
terminal record that this census can read, so discarded runs land in
`no_terminal_record` (or `complete_unpromoted`) rather than a stratum of their
own. Outcome precedence: the `promote(<run>): <verdict>` commit on the
project's primary branch, else the manifest's `status:` line.

## Headline: record-keeping against product code

**Of the 12,154 edits classified as product or record-keeping, 5,361 (44.1%)
are record-keeping** -- plan/evidence/research HTML 1,681, manifest and report
files 3,254, figures 426 -- against 6,793 product edits (source 4,701, tests
2,092). A further 1,519 edits classify as neither (config, data, notebooks).

**14.3% of all tool calls (10,049 of 70,090) read record-keeping material**:
plan/evidence/research HTML 5,957, manifests 2,048, skill files 1,444,
`AGENTS.md`/`CLAUDE.md` 600.

So roughly two in five file edits, and one in seven tool calls, go to the
record rather than to the code it describes.

## Per lane and role

| cell | n | first-request input tokens | turns | first edit at turn | record reads (pooled share) | gate/test wall share (pooled) |
|---|---|---|---|---|---|---|
| clive/implement | 889 | 65,611 / 81,345 | 88 / 198 | 30 / 59 | 10.6% | 0.42% |
| clive/review | 772 | 64,844 / 80,749 | 46.5 / 78 | 36 / 61 | 6.0% | 0.06% |
| clive/documentation | 73 | 72,827 / 118,684 | 56 / 122 | 32 / 66 | 20.5% | 0.01% |
| clive/investigate | 88 | 66,535 / 80,152 | 56.5 / 104 | 30 / 61 | 8.0% | 0.0% |
| clive/test | 33 | 64,458 / 81,235 | 54 / 98 | 32 / 48 | 8.8% | 0.03% |
| codex/implement | 232 | 68,032 / 163,571 (n=8) | 14 / 42 | 2 / 4 | 16.0% | 0.11% (n=8) |
| codex/review | 94 | 65,518 / 160,885 (n=7) | 5 / 17 | 2 / 3 | 18.5% | 0.14% (n=7) |

Read the table by its denominators, not its blanks:

- **First-request input tokens is unmeasurable for the codex grammar.** The
  codex dialect publishes only one `turn.completed` usage summing the whole
  turn, so 324 of 326 codex-lane runs have no first-request figure; the n=8
  cells are the codex-lane runs served on the claude grammar. The census
  declines to publish a contaminated number (the same call
  `carryover_census` makes) instead of labelling a whole-run total "first".
- **Gate/test wall share is claude-grammar only** (the codex dialect emits no
  timestamps), and it is small: 0.42% of observed wall time pooled across
  clive/implement. Gate commands are run rarely and briefly; most wall time is
  reading, thinking and editing.
- **Self-verification reads are near zero.** clive/implement median 0, p90 2
  (n=882): workers rarely re-read a file they just wrote. Only 8 codex-lane
  runs are measurable in this cell, median 0.5, p90 13.

## Two different shapes of worker

The clearest structural finding is the turn shape, not the table's magnitudes:

- **clive-dialect runs read before they write** -- clive/implement reaches its
  first edit at median turn 30 of 88.
- **codex-dialect runs write before they read** -- codex/implement is at its
  first edit by turn 2, and codex/review by turn 2, finishing in a median of 14
  and 5 assistant turns against clive's 88 and 46.5.

Both reach the same promotion outcomes (`promoted_passed` 1,579 overall), so
the census states the shapes; which shape produces the better work is §8's
synthesis, not this node's measure.

## Per-outcome strata

`outcome_strata` in the JSON carries turns, tool calls, first-edit turn,
first-request input and record-read share per lane/role/outcome cell (48
cells). Cells are thin where outcomes are rare -- e.g. `clive/implement/blocked`
n=1, `codex/implement/blocked` n=1 -- and every cell names its `n`, so a
reader can weigh each.

## Method notes a reader needs

- Window filter is on `dispatched_at`, so a run dispatched at 09:52 and
  completed after the capture instant is in; its stream is read whole.
- The run population is pinned to `population.json` (built once from the run
  store and the five ledgers). The store grows after the capture instant as
  late runs reconcile, which was measured here: an unpinned re-run two minutes
  apart differed (2,325 -> 2,326 runs). With the pin, the script re-runs
  byte-identical -- verified twice, `cmp` clean.
- The parser is proved on one named healthy run per schema, both
  `promoted_passed`: claude grammar
  `r-20260914T160220568583-cca-solve-profile-kernel-count` parses to 319
  assistant turns, 64 tool calls, first edit at turn 112, 2 source edits and 7
  manifest and report edits; codex grammar
  `r-20260915T041619672372-cca-private-region-figure-and-wedge-audit` parses
  to 19 turns, 190 tool calls, first edit at turn 2, 10 source edits, 4 figure
  edits. The script refuses to write output if either proof run parses empty.
- The reader is `anatomy_census.py` in this directory; `run.log` is one
  complete run. `population.json` is the pinned input.
- Lane is the run record's `agent.backend`, `codex-*` folded to `codex`.
  Dialect is the stream grammar, and the two do not coincide: the codex lane
  has 343 codex-dialect runs and 23 claude-dialect, clive has 1,847 claude and
  9 codex-dialect.

## Where this node's reading is thinner

- "Record-keeping read" counts tool calls whose target resolves to one of the
  four categories above; a call that reads several files counts once if any
  target is record-keeping, so the read share is a floor, not a ceiling.
- The edit-class shares treat `docs/research/data/` output (census JSON,
  findings) as record-keeping, which is this census's "record-keeping
  against product code" asks; a reader who defines data artifacts as product
  should recompute from `pooled.edits_by_class`.