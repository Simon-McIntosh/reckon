# Coordinator overhead census

The fixed window is 2026-09-12T00:00:00Z through 2026-09-26T10:00:00Z, inclusive. The census identifies **2,182 promoted runs** among 2,328 run records across five primary branches. The 38 available coordinator transcripts account for 1,789 promotions and 38,699 distinct assistant API responses: **21.63 responses per promoted run**.

A promoted run is an administrative landing, not necessarily useful product work. 118 have gate `failed`; 497 have gate `not-run`; 1,567 have gate `passed`. Role breakdown: cleanup 1, documentation 89, implement 1,040, investigate 108, review 908, test 36. Distinct run IDs are the node denominator; repairs and retries with different IDs remain separate.

## The five largest activity costs per promoted run

The common unit here is observed tool calls per promoted run, not elapsed time or money. One shell call can contain several commands and one response can emit several calls. The classifier assigns each call once; the unclassified remainder is visible rather than distributed by guesswork.

| Rank | Activity | Calls | Calls / promoted run | Denominator |
| ---: | --- | ---: | ---: | ---: |
| 1 | Other tools and uncategorised shell work | 11,862 | 6.631 | 1,789 |
| 2 | Git and merges | 5,604 | 3.132 | 1,789 |
| 3 | Reading diffs, manifests and gate logs | 4,881 | 2.728 | 1,789 |
| 4 | Crew reads and MCP views | 4,776 | 2.670 | 1,789 |
| 5 | Dispatch, resume and redispatch | 2,861 | 1.599 | 1,789 |

Remaining categories: Promotion 2,088 calls / 1,789 promotions = 1.167; Plan, evidence and research edits 1,816 calls / 1,789 promotions = 1.015; Follower and monitor tool calls 1,236 calls / 1,789 promotions = 0.691.

Separately, 3,201 incoming obligation/fleet notification records were observed (1.789 per covered promotion). These are incoming records, not added to the exclusive tool-call total. They may repeat the same obligation.

## Context volume and the five most expensive sessions

Total logical input is 20,375,175,527 tokens (11,389,142 per covered promotion), of which 20,179,819,699 are cache reads (99.04%). Output is 28,029,284 tokens (15,668 per promotion). Logical input includes uncached input, cache creation and cache reads. These figures are not billable tokens, dollars, active work time, or unique text. No monetary spend is derived without a price/usage receipt.

The session ranking uses logical input tokens per promotion. Whole-session work inside the window is counted, including non-crew work; small denominators and mixed responsibilities can dominate.

| Session / project | Promotions | API responses / promotion | Input tokens / promotion | Output tokens / promotion |
| --- | ---: | ---: | ---: | ---: |
| `2f9d3191-e884-4a53-99ab-b894325cccbd` / imas-ambix | 19 | 63.63 | 33,379,000 | 66,336 |
| `df0e98d4-1e76-4caf-90cf-12f74386be2a` / nova | 8 | 61.62 | 31,067,077 | 34,483 |
| `aeb652ee-2fb5-42fc-b5dc-1235d9ca4a0d` / imas-ambix | 9 | 61.00 | 29,950,860 | 66,418 |
| `53867a4c-f11b-48da-85bc-d43d6d8815dd` / nova | 15 | 50.53 | 27,930,549 | 34,608 |
| `9a2a0944-44ee-4d69-8147-14071cf6a5e9` / imas-codex | 28 | 52.18 | 27,283,415 | 40,572 |

The [session scatter plot](/reckon/figures/orchestrator-crew-pattern-studies/coordinator-effort.svg) shows the relationship between promotion count and logical input per promotion. Its horizontal line is the pooled observed rate, not a fitted causal model.

## Refusals and recovery

| Operation | Observed refusals | Positive success receipts | Recovered before window end | Mean responses to next family success | Same-target recoveries | Mean responses to same-target success |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| dispatch | 320 | 778 | 293 / 320 | 76.69 | 111 | 12.50 |
| promotion | 338 | 486 | 281 / 338 | 92.22 | 107 | 14.83 |

Dispatch error codes: `cli-error` 76; `not-dispatchable` 71; `scope-conflict` 40; `dispatch-refused` 32; `unspecified` 28; `member-in-flight` 21; `unreconciled-runs` 18; `watcher-required` 17; `competence-refusal` 7; `plan-unavailable` 7; `budget-hold` 3.

Promotion reason groups: `commit-or-change-record` 67; `missing-gate-receipt` 52; `negative-control` 41; `plan-implementation-not-advanced` 39; `other-refusal` 38; `scope-or-boundary` 30; `review-required-or-stale` 25; `resume-path` 15; `live-process` 13; `unresolvable-commit` 9; `ledger-or-git-state` 6; `manifest-record` 3.

Exact refusal text, source file/line, timestamp and recovery interval are retained in the hashed attempts artifact. Reason groups are text classifications; `cli-error` means the CLI emitted an Error line without a structured code. A positive receipt may be full JSON, a printed Python dictionary, or a clipped success marker; receipt shapes are counted in the JSON. A quiet shell is never counted as success. The next success in an operation family need not retry the same node; same-target columns require a literal target join. Missing successes and unidentified targets stay null. Intervals overlap and contain other work, so their sums cannot be interpreted as wasted labor.

**Coverage limit:** 2,896 candidate attempt calls have no classifiable receipt or have ambiguous mixed command families. Shell clipping, redirection, helper scripts and unavailable output prevent an exhaustive refusal count; these figures are observed lower bounds. Calls to help and dry runs are excluded from attempt accounting.

## Documentation merge conflicts

| Primary branch | Merges touching plans/evidence | Replay conflicts anywhere | Replay conflicts in plans/evidence | Unmeasured |
| --- | ---: | ---: | ---: | ---: |
| imas-ambix/main | 111 | 54 | 54 | 0 |
| imas-codex/main | 173 | 56 | 56 | 0 |
| imas-efit/develop | 124 | 40 | 29 | 0 |
| nova/main | 229 | 99 | 99 | 0 |
| reckon/main | 449 | 177 | 177 | 0 |

Across 1,086 qualifying merges, 426 (39.23%) conflict somewhere when their recorded parents are replayed; 415 (38.21%) conflict in the named documentation paths. These are current `git merge-tree` replays, not a count of historical manual resolutions. The sample is first-parent primary-branch merges whose resulting tree differs from the first parent under docs/plans or docs/evidence; merges with no net documentation change are excluded. Conflict exits and operational errors are distinguished. Object writes are redirected to isolated temporary directories.

## Missing coverage and unmeasured work

- 14 of 52 recorded coordinator sessions lack usable transcript evidence: 13 files are missing and 1 snapshot has no in-window records. They account for 393 of 2,182 promotions. Their turns/tokens are null and excluded from the cost denominator.
- 0 run records lack a runtime session ID, in 0 unattributed groups. Every observed runtime identity came from node_definition.coordinator.runtime_session_id. Top-level worker session IDs were not substituted.
- All selected coordinator harness records identify the message-style session format. Worker `thread.started` streams are not inputs to this study, so no claim of their emptiness or activity is made.
- **Hand-committed worker diffs: not measured.** Successful coordinator commit calls are retained as candidates, but a commit receipt alone does not establish worker authorship. Quiet commits, shell variables, copied patches and edits made on the primary checkout require an additional provenance join. The reported count is null; zero matches to the narrow explicit-worktree command probe is not evidence that hand integrations were absent.
- Tool classification is deterministic and exclusive but heuristic. Shell commands are tokenized so quoted goals/advice do not masquerade as executed commands. Compound calls get one category, not one count per subprocess. No duration or token cost is assigned to individual tool categories.

## Reproduction and evidence

Run from this directory with the project's root virtual environment:

```bash
/home/ITER/mcintos/Code/reckon/.venv/bin/python census.py
/home/ITER/mcintos/Code/reckon/.venv/bin/python verify.py --rerun
```

The first command reparses fixed transcript snapshots and reruns merge-tree against pinned parent objects. The second checks recorded positive controls, coverage invariants and SHA-256 identities, then compares a fresh census byte for byte. `inputs.json` pins transcript byte extents, source hashes, branch revisions and frozen inputs under the durable run directory. `overhead.json` holds compact session/totals data and absolute path/hash references to larger artifacts. These external files must be retained; this is not a standalone corpus archive inside Git. `census.log` and `verification.log` name the tested revision and command. All committed files remain below 300 KB. `render.py` regenerates this report and the plot from the measured JSON.

The incomplete inherited draft was preserved before repair. A known duplicate transcript message is asserted as 111,913 input and 197 output tokens once, despite repeated content records. A real missing-gate refusal exercises the Error-line parser; both clean and conflicting merge replays are retained as positive controls. No mutation negative control applies to this measurement node; byte-identical re-execution is its declared gate.

The next concrete action is coordinator integration and independent verification at the merged head. For a complete hand-integration census, commission the worker-diff/primary-commit provenance join using the retained candidate artifact. Recovery of missing or empty transcript evidence would extend coverage; it must not silently change this frozen sample.
