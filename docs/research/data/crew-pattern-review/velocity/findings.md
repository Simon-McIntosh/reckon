# Crew velocity census

The five primary branches received **299,492 product additions** (source plus tests) in 6,515 first-parent commits during 2026-09-12 00:00 UTC through 2026-09-26 10:00 UTC. Of the 123,960 additions with a full seven-day follow-up, 3,706 were deleted within seven days (2.99%), leaving **120,254 seven-day durable additions**. Another 175,532 additions lack full follow-up. These are line identity counts, not a judgment about usefulness or correctness.

There are 2,303 promotion records: 1,259 implement-class and 1,044 review/investigate. Linking retry/repair families yields 2,268 logical nodes (1,250 implement-class; 1,018 review/investigate). Across 1,107 commit-bearing landed logical nodes, 1,788 recorded attempts imply **1.62 attempts per landed node**. This is a lower bound: discarded attempts absent from committed ledgers and repairs without a declared or literal name link are not invented.

Record additions are 14,704,768 lines versus 299,492 product additions: **49.10 record lines per product line**. The record bucket is plan/evidence/research HTML, the figures directory, and docs/state. Binary figures are counted as file changes; their line counts stay unavailable. The figures directory alone includes 11,194,555 support-data lines. Excluding those gives 11.72; excluding the entire figure directory gives 5.35 (1,601,335/299,492). The inclusive ratio measures stored output volume, not writing effort. Other documentation, configuration, lockfiles and data outside figures are a separate bucket.

Dispatch-to-completion median/p75 are **43.78 / 87.68 minutes** (n=2,303/2,303). Dispatch-to-promotion median/p75 are **83.03 / 186.44 minutes** (n=2,303/2,303). Completion uses the ledger clock; promotion uses the earliest reachable promote(run-id) commit's committer clock where present, otherwise the first primary commit adding the run record, which is an upper bound on the promotion event. Promotion is not evidence that product code survived a merge. Clock sources: {"first_primary_ledger_appearance_upper_bound": 104, "promote_commit": 2199}.

The timed, mature, attributed product cohort delivered **116.88 durable lines per worker-hour**: 86,478 durable lines / 739.90 recorded worker-hours from 308 runs. 24,997 eligible attributed additions lack a fully matched timed promotion cohort. This matched rate uses producing runs, not all review/failure overhead. The JSON also records the gross rate using all promoted worker-hours.

## Comparison with the published August baseline

The [August review](/reckon/research/crew-fleet-rox-review#s2) covers August 12-16, four projects, and 259 completed runs. Its figures are quoted, not recomputed. September adds imas-efit and measures primary-branch output, so the table does not imply an efficiency gain from larger totals.

| Measure | Published August | September census | Comparison limit |
|---|---:|---:|---|
| Completed/promoted records | 259 completed | 2,303 promoted | Five calendar days/four projects versus 14 days 10 hours/five projects |
| Lines added | 108,587; three projects reporting | 299,492 source + test additions | August authored lines were not classified or restricted to primary landings |
| Commits | 228 | 6,515 first-parent commits | September includes record and merge commits |
| Median minutes per run | 9.8 | 43.78 dispatch to completion | Clock sources and role mix differ; n=2,303 |
| Worker-hours | 66.2, stall corrected | 2,941.57, recorded | September is not stall corrected; n=2,230 |
| Median authored changed lines per worker-minute | 16-24 across three codebases | Not a comparable metric | Durable landed additions/hour is a different numerator, statistic and cohort |
| Durable lines per worker-hour | Not reported | 116.88 | No August durability denominator |
| Seven-day deletion share | Not reported | 2.99% | Only mature September additions enter denominator |
| Record/product line ratio | Not reported | 49.10 | No August class split |
| Promotion latency; attempts/landed node | Not reported | 83.03 min median; 1.62 attempts | No comparable August figures |

## Project comparison

| Project | September promoted records | August completed records | September product adds | August reported adds | Mature adds deleted within 7d | Durable mature adds |
|---|---:|---:|---:|---:|---:|---:|
| reckon | 1,107 | 80 | 125,343 | 9145 | 665/28,713 | 28,048 |
| imas-ambix | 264 | 45 | 33,124 | 45877 | 586/8,553 | 7,967 |
| nova | 441 | 116 | 104,442 | 53565 | 2,074/69,552 | 67,478 |
| imas-efit | 248 | not included | 23,039 | not reported | 267/9,308 | 9,041 |
| imas-codex | 243 | 18 | 13,544 | not reported | 114/7,834 | 7,720 |

## Lane comparison

| Lane | Promoted implement records | Promoted review/investigate records | Attributed product adds | Mature durable adds | Matched durable lines / worker-hours |
|---|---:|---:|---:|---:|---:|
| clive | 985 | 855 | 202,249 | 65,349 | 65,349 / 709.55 = 92.10 |
| codex | 246 | 119 | 74,329 | 42,505 | 21,129 / 30.35 = 696.13 |
| claude | 28 | 67 | 1,477 | 0 | 0 / 0.00 = unmeasured |
| native | 0 | 3 | 0 | 0 | 0 / 0.00 = unmeasured |
| mixed | 0 | 0 | 3,319 | 2,652 | 0 / 0.00 = unmeasured |
| unattributed | 0 | 0 | 18,118 | 9,748 | 0 / 0.00 = unmeasured |

The [durable-output figure](/reckon/figures/crew-pattern-review-velocity/durable-product-lines.png) shows the mature daily series and gross additions beneath it. September 19 has only a partial eligible cohort; later days are censored, not zero durable output.

## Denominators and limits

- Each project, UTC day and lane has a cell, including explicit zero-population cells. Run days are promotion days; line days are primary landing days. Timing cells are cohorts, not calendar-time exposure.
- Primary first-parent diffs count a merge once. Worker commit citations assign the entire primary landing diff to one lane, mixed lanes or unattributed. This measures an integration footprint, including merge resolution; it does not prove authorship of each line. Promotion/release/record commits with an explicit run id inherit that run's lane.
- Lane changes use the final recorded backend. The JSON retains lineage so cross-lane work can be inspected rather than mistaken for a controlled comparison. Differences between lanes also reflect project, role and workload selection.
- Line identity is tracked through insertions, edits and renames. Re-added text is a new line identity. Deletion is a churn measure, not a defect verdict; intentional simplification also deletes lines.
- The ledger supplies 2,230/2,303 worker-time observations. Sources: {"stream_events": 1925, "wall_fallback": 305}. Nonpositive durations are excluded; no missing duration is filled with zero.
- 11 rows report zero worker seconds, including 1 with commit citations. Their ids remain in the JSON; a zero counter is not credited as free product work.
- Coordinator active dispatch is 1,099.97 hours over 54 recorded project/session pairs and 2,303/2,303 promotion records. It unions worker intervals per session and sums sessions; it is not measured human or coordinator interaction time. Gross attributed product/active-dispatch-hour is 255.80; the August report did not report this denominator.
- The predecessor_run field is not used as an attempt link: the application can infer it from a matching base commit, which is integration ancestry rather than retry evidence.
- Missing promotion clocks and unresolved/unreachable citations are listed by run in summary.json.coverage; they are not guessed from timestamps, author names or prose.

  - reckon: 1,107 available records completed in the window; 0 lack a recoverable promotion clock; 6 contain unresolved or unreachable commit citations; 0 rows were recovered from SQLite for independently reachable promotion commits; 0 promotion ids still lack any record; 56 clocks come from first committed ledger appearance rather than an explicit promote commit.
  - imas-ambix: 264 available records completed in the window; 0 lack a recoverable promotion clock; 0 contain unresolved or unreachable commit citations; 0 rows were recovered from SQLite for independently reachable promotion commits; 0 promotion ids still lack any record; 4 clocks come from first committed ledger appearance rather than an explicit promote commit.
  - nova: 441 available records completed in the window; 0 lack a recoverable promotion clock; 19 contain unresolved or unreachable commit citations; 1 rows were recovered from SQLite for independently reachable promotion commits; 0 promotion ids still lack any record; 16 clocks come from first committed ledger appearance rather than an explicit promote commit.
  - imas-efit: 248 available records completed in the window; 0 lack a recoverable promotion clock; 1 contain unresolved or unreachable commit citations; 82 rows were recovered from SQLite for independently reachable promotion commits; 0 promotion ids still lack any record; 0 clocks come from first committed ledger appearance rather than an explicit promote commit.
  - imas-codex: 243 available records completed in the window; 0 lack a recoverable promotion clock; 2 contain unresolved or unreachable commit citations; 0 rows were recovered from SQLite for independently reachable promotion commits; 0 promotion ids still lack any record; 28 clocks come from first committed ledger appearance rather than an explicit promote commit.

## Reproduction and checks

The full `velocity.json` and `inputs.json.gz` live in `/home/ITER/mcintos/.config/reckon/crew/runs/r-20260926T104517371666-velocity-census/`. The repository keeps `summary.json` under 300 KB with all cited figures, project/lane/week aggregates and named coverage rows. `inputs.json.gz` freezes reduced ledger records, primary heads, commit metadata, numeric file deltas and edit coordinates. It contains no transcript bodies or source text. A normal census run reads only this snapshot; `--capture` refuses to overwrite it.

```sh
/home/ITER/mcintos/Code/reckon/.venv/bin/python docs/research/data/crew-pattern-review/velocity/census.py
/home/ITER/mcintos/Code/reckon/.venv/bin/python docs/research/data/crew-pattern-review/velocity/verify.py
/home/ITER/mcintos/Code/reckon/.venv/bin/python docs/research/data/crew-pattern-review/velocity/plot.py
```

The verifier checks byte equality of both the full file in the run directory and compact file in the repository, the 300 KB compact bound, conservation across project/day/lane/week partitions, all five projects present, nonzero observed churn and censoring, and known line-identity controls. The full capture checks every primary product commit against its numstat addition count. The initial capture was rejected when zero-context diff matching disagreed with normal numstat (30/23 versus 28/21 on a real file). The corrected instrument parses normal-context diffs; both the failed capture and its replacement logs are retained with the run.

Pinned primary heads:

- reckon `main`: `056c08e0d7c96a5b4edaee5dbcfe169c0c5ceaf9`
- imas-ambix `main`: `053513decf20d76c58c2beac1e5636514d8029a2`
- nova `main`: `16287918db2f90d0088c1786ec45965cd90e6652`
- imas-efit `develop`: `1592988bb3ff9939868a1ca374a9018af3d8af72`
- imas-codex `main`: `26723f7a057904323b693699b75771692cce46ba`

Canonical input SHA-256: `5172e82376f8d20158bb65c413dfa1b2f762f7bee1883926d2db0d99fdbab479`.
