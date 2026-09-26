# Code depth, interface width and reuse

Reckon grew rapidly while its overall clone share fell; its crew subset grew faster and became more duplicated. Nova grew more slowly, but its clone share rose. Neither trend establishes causation by a worker lane or a change in scientific correctness. The useful result is a ranked set of shared implementation opportunities, not a claim that every small function is shallow.

The census measures eight pinned repository snapshots and twelve scope rows. `reckon/crew/` is a subset of `reckon/`, not a third repository. All paths and line numbers below resolve at the named snapshot commit, not at a moving branch head.

## Snapshot identities and time offsets

Selection uses the nearest committer timestamp on pinned primary-branch first-parent history, limited to the capture cutoff of 2026-09-26 10:00Z. Weekly labels are targets, not claims that a commit exists exactly at noon. This explicitly bounds the September 26 noon target, which is later than capture. All source reads use git archive and temporary extracted Python files; neither target package is executed.

| Repository | Noon target | Commit | Actual commit time | Offset, hours |
|---|---|---|---|---:|
| reckon | 2026-09-05 | `7b46b7b56f52d92a324888b1207af7833b425c58` | 2026-09-05T15:27:16+02:00 | +1.45 |
| reckon | 2026-09-12 | `2080a7313945676fc08091cfcb6a8716b2f40c9a` | 2026-09-14T07:59:34+02:00 | +41.99 |
| reckon | 2026-09-19 | `23635a769a9cd9b13799de53c0b66c2b762abe88` | 2026-09-19T14:09:33+02:00 | +0.16 |
| reckon | 2026-09-26 | `056c08e0d7c96a5b4edaee5dbcfe169c0c5ceaf9` | 2026-09-26T11:59:44+02:00 | -2.00 |
| nova | 2026-09-05 | `e247cff66a06bc6a12cdfd3d9003c34fde90ecde` | 2026-09-05T14:07:25+02:00 | +0.12 |
| nova | 2026-09-12 | `9062b6f121e4355ffd091280c4e5df3905943130` | 2026-09-11T15:09:24+02:00 | -22.84 |
| nova | 2026-09-19 | `c11eea7f110bdec79d586e23ebc83f5568b3f7ab` | 2026-09-19T14:00:38+02:00 | +0.01 |
| nova | 2026-09-26 | `16287918db2f90d0088c1786ec45965cd90e6652` | 2026-09-24T17:59:59+02:00 | -44.00 |

Reckon's September 12 target lands on September 14; nova's September 26 target lands on September 24. Growth rates are therefore comparisons between the reported snapshots, not uniform seven-day throughput measurements.

## Size, surface and implementation lengths

Source/test lines below are physical Python lines, including comments, blank lines and docstrings. The population includes package-contained scripts but excludes top-level scripts and other languages. Public functions include public methods under public classes; public classes include nested public classes. These are definitions, including separate property accessors, not measured exports or unique API attributes. Reexports, instance fields and dynamic registrations are outside this definition.

| Scope | Target | Source lines | Test lines | Modules | Public functions / classes | Function median / p90 | Pure forwarders / all functions |
|---|---|---:|---:|---:|---:|---:|---:|
| reckon/ | 09-05 | 45,498 | 66,492 | 48 | 439 / 117 | 16 / 63 | 9 / 1274 |
| reckon/crew/ | 09-05 | 13,268 | 33,698 | 13 | 113 / 18 | 18 / 65 | 3 / 378 |
| reckon/ | 09-12 | 54,908 | 92,707 | 56 | 497 / 132 | 16 / 61 | 14 / 1543 |
| reckon/crew/ | 09-12 | 19,454 | 54,564 | 20 | 146 / 31 | 18 / 64.4 | 5 / 547 |
| reckon/ | 09-19 | 62,390 | 112,691 | 59 | 543 / 135 | 16.5 / 62 | 16 / 1748 |
| reckon/crew/ | 09-19 | 23,329 | 67,709 | 21 | 166 / 33 | 18 / 65 | 5 / 654 |
| reckon/ | 09-26 | 89,711 | 173,557 | 88 | 843 / 174 | 16 / 56 | 21 / 2621 |
| reckon/crew/ | 09-26 | 40,343 | 114,786 | 40 | 366 / 61 | 18 / 57 | 8 / 1171 |
| nova/ | 09-05 | 157,127 | 109,530 | 428 | 4382 / 990 | 8 / 41 | 100 / 6539 |
| nova/ | 09-12 | 171,484 | 124,950 | 447 | 4546 / 1034 | 9 / 43 | 104 / 6950 |
| nova/ | 09-19 | 177,929 | 133,796 | 450 | 4614 / 1053 | 9 / 44 | 105 / 7149 |
| nova/ | 09-26 | 179,742 | 143,554 | 450 | 4625 / 1054 | 9 / 44 | 108 / 7245 |

Crew test lines count whole files importing or naming `reckon.crew`; they overlap the total and can test additional concerns. Function length is the inclusive def-to-end physical span, including docstrings and excluding decorators. The compact JSON also carries min, p75, p95 and max; the full run-directory JSON retains the complete length histogram. A pure forwarder has one call/return after removing its docstring and passes each argument once without transformations; import-and-forward wrappers are deliberately outside that strict count.

At the last snapshot, pure forwarders are 21/2,621 reckon functions (0.80%), 8/1,171 crew functions (0.68%), and 108/7,245 nova functions (1.49%). Those denominators include private and nested functions. This narrow instrument does not support a claim that these codebases consist mainly of trivial wrappers.

## Clone share and interface trend

The clone numerator is the union of token-bearing source lines participating in repeated six-line windows, counting both copies once. The denominator excludes blank lines, comments and docstrings. Whitespace and string/number values are normalized; identifiers and operators are preserved. Copies must be non-overlapping. Longer blocks appear as overlapping windows but their covered lines are unioned. This conservative detector misses renamed-variable and semantic clones, and literal normalization can group unrelated logic: matches require review before deletion.

| Scope | Target | Cloned code lines / all code lines | Clone share | Weekly change, percentage points | Public surface | Weekly change |
|---|---|---:|---:|---:|---:|---:|
| reckon/ | 09-05 | 4,246 / 36,127 | 11.753% | baseline | 556 | baseline |
| reckon/ | 09-12 | 4,904 / 42,305 | 11.592% | -0.161 | 629 | +73 |
| reckon/ | 09-19 | 5,343 / 47,065 | 11.352% | -0.240 | 678 | +49 |
| reckon/ | 09-26 | 6,843 / 63,806 | 10.725% | -0.628 | 1,017 | +339 |
| reckon/crew/ | 09-05 | 563 / 10,254 | 5.491% | baseline | 131 | baseline |
| reckon/crew/ | 09-12 | 942 / 14,331 | 6.573% | +1.083 | 177 | +46 |
| reckon/crew/ | 09-19 | 1,019 / 16,592 | 6.142% | -0.432 | 199 | +22 |
| reckon/crew/ | 09-26 | 1,885 / 26,721 | 7.054% | +0.913 | 427 | +228 |
| nova/ | 09-05 | 11,521 / 110,575 | 10.419% | baseline | 5,372 | baseline |
| nova/ | 09-12 | 13,523 / 121,672 | 11.114% | +0.695 | 5,580 | +208 |
| nova/ | 09-19 | 14,395 / 126,459 | 11.383% | +0.269 | 5,667 | +87 |
| nova/ | 09-26 | 14,402 / 127,865 | 11.263% | -0.120 | 5,679 | +12 |

Overall, reckon clone share falls 1.028 percentage points while public surface grows 82.9%; crew clone share rises 1.564 points while public surface grows 226.0%; nova clone share rises 0.844 points while public surface grows 5.7%. Reckon's cloned lines still grow from 4,246 to 6,843 despite its falling share. A falling percentage is not shrinking duplicate maintenance work.

| Reckon target | CLI commands | CLI groups | CLI option occurrences | Distinct flags | MCP tool/view pairs | Distinct MCP names | Coded refusal families |
|---|---:|---:|---:|---:|---:|---:|---:|
| 09-05 | 50 | 7 | 202 | 96 | 26 | 17 | 0 |
| 09-12 | 50 | 7 | 209 | 98 | 27 | 18 | 22 |
| 09-19 | 51 | 7 | 216 | 101 | 27 | 18 | 22 |
| 09-26 | 56 | 7 | 239 | 112 | 30 | 20 | 23 |

Reckon's CLI command changes are 0, +1, +5 week over week; option changes are +7, +7, +23; MCP tool/view changes are +1, 0, +3. The crew directory itself has no command decorators or MCP view declarations because those live in the parent package's CLI/MCP modules; that zero does not mean crew has no interface. The coded-refusal count measures `format_refusal` families, not all guard conditions. The first snapshot predates those codes but has dispatch raise sites, retained in JSON. The real PID-existence primitive is detected once at the final reckon snapshot, so it does not enter a three-copy reimplementation group.

Nova's incidental Click surface is unchanged at three commands and fourteen option occurrences; its relevant width signal here is public definitions, growing +208, +87, +12. Its MCP and coded crew-refusal counts are zero and are not used as measures of its scientific API.

## Modules with wide public surfaces relative to implementation

Ranked below within each repository by public definitions per 100 token-bearing lines, requiring at least five public definitions. These are review leads: protocol declarations, schema models, property accessors and service facades can be appropriately small.

| Repository | Module | Public definitions | Code lines | Public definitions per 100 code lines |
|---|---|---:|---:|---:|
| reckon | `reckon/service.py` | 14 | 125 | 11.20 |
| reckon | `reckon/_flight_schema.py` | 31 | 294 | 10.54 |
| reckon | `reckon/crew/pace.py` | 11 | 119 | 9.24 |
| reckon | `reckon/_mcp_tools.py` | 10 | 143 | 6.99 |
| reckon | `reckon/crew/reserve.py` | 5 | 77 | 6.49 |
| nova | `nova/io/streaming.py` | 6 | 16 | 37.50 |
| nova | `nova/frame/dataarray.py` | 9 | 33 | 27.27 |
| nova | `nova/frame/error.py` | 5 | 21 | 23.81 |
| nova | `nova/frame/plasmaloc.py` | 15 | 65 | 23.08 |
| nova | `nova/thermalhydralic/sultan/sample.py` | 22 | 96 | 22.92 |

## Ten consolidation candidates, ranked by expected value

Ranking prioritizes behavior that controls persistence, accounting or numerical contracts, then copy count and migration complexity. It is engineering judgment, not a measured savings estimate. The complete machine census retains every group found by its four disclosed detectors: direct primitive use, repeated module-level helper names, identical AST bodies and six-line normalized bodies. The detectors overlap; group counts cannot be summed as unique concepts. Primitive-use groups include clients, and same-name groups can be unrelated. Semantic equivalence outside those detectors is unmeasured.

### 1. One UTC timestamp parser for reckon

44 functions call fromisoformat directly. Parsing affects quota windows, freshness and resume eligibility, so policy drift is more costly than these functions' size. Extract parsing only; elapsed-time and display policy remain with their callers. Proposed single home: `reckon/_timestamps.py:parse_utc (new primitive module)`.

Constraint: Specify malformed-input behavior, naive timestamps, Z suffixes, offsets and numeric epoch handling before migration. Some of these 44 are consumers with inline parsing, not interchangeable whole functions.

Copies (44 definitions, commit `056c08e0d7c96a5b4edaee5dbcfe169c0c5ceaf9`):

- `reckon/_backends.py:603` — `_parse_cached_fetch_stamp`.
- `reckon/_backends.py:2469` — `_event_timestamp`.
- `reckon/budget.py:261` — `_parse_stamp`.
- `reckon/cli.py:2587` — `_follow_row_stamp`.
- `reckon/crew/budget_group.py:359` — `_observed_moment`.
- `reckon/crew/dispatch.py:366` — `_actionable_budget_hold`.
- `reckon/crew/dispatch.py:2204` — `_lane_advisory_instant`.
- `reckon/crew/dispatch.py:2317` — `_lane_reading_carry`.
- `reckon/crew/hold.py:185` — `_as_moment`.
- `reckon/crew/lane_document.py:139` — `_parse_stamp`.
- `reckon/crew/lane_evidence.py:200` — `_moment`.
- `reckon/crew/metering.py:468` — `_event_timestamp`.
- `reckon/crew/metering.py:477` — `_stamped_elapsed`.
- `reckon/crew/obligations.py:36` — `_seconds_since`.
- `reckon/crew/paid_lanes.py:231` — `_parse_stamp`.
- `reckon/crew/promotion.py:1658` — `_elapsed_seconds`.
- `reckon/crew/promotion.py:1672` — `_assume_utc_if_naive`.
- `reckon/crew/promotion.py:2240` — `_terminal_stream_data`.
- `reckon/crew/query.py:586` — `_normalize_stamp`.
- `reckon/crew/quota_weight.py:63` — `_as_date`.
- `reckon/crew/recovery.py:1378` — `_budget_timing`.
- `reckon/crew/recovery.py:2860` — `_attempt_started_seconds`.
- `reckon/crew/recovery.py:2943` — `_manifest_wait`.
- `reckon/crew/recovery.py:3148` — `_run_chain_manifest_freshness`.
- `reckon/crew/resumption.py:119` — `_parse_stamp`.
- `reckon/crew/rollout.py:229` — `_parse_timestamp`.
- `reckon/crew/routing.py:1445` — `_parse_utc_timestamp`.
- `reckon/crew/runs.py:2810` — `_stream_quiet_seconds`.
- `reckon/crew/summary.py:515` — `_row_moment`.
- `reckon/crew/ticker.py:483` — `local_clock`.
- `reckon/crew/window_reading.py:249` — `_parse_stamp`.
- `reckon/doccheck.py:203` — `modified_age_days`.
- `reckon/flight.py:1198` — `_observation_age`.
- `reckon/hooks/worker_stop.py:115` — `_manifest_predates_attempt`.
- `reckon/ledger.py:2174` — `_event_completion`.
- `reckon/ledger.py:2214` — `_worker_seconds`.
- `reckon/ledger.py:2328` — `_parse_timestamp`.
- `reckon/mcp_views.py:95` — `_parsed_observation`.
- `reckon/mcp_views.py:970` — `compose_review`.
- `reckon/project_state.py:521` — `_review_date`.
- `reckon/project_state.py:545` — `_validate_review`.
- `reckon/schedule.py:35` — `_stamp_millis`.
- `reckon/serve.py:527` — `_elapsed_since`.
- `reckon/sprint_liveness.py:79` — `_observed_seconds`.

### 2. One atomic JSON persistence primitive for reckon

14 functions independently serialize JSON and replace a file. The existing writers differ in temporary naming, cleanup and fsync. One primitive would concentrate interrupted-write behavior and concurrency verification. Proposed single home: `reckon/_store.py:write_json_atomically (new primitive beside existing envelope writer)`.

Constraint: Keep envelope construction, pointer launcher-host stamping and response validation outside the writer; preserve each caller's locking and durability requirements. The layout migration also moves non-JSON files and is not replaced wholesale.

Copies (14 definitions, commit `056c08e0d7c96a5b4edaee5dbcfe169c0c5ceaf9`):

- `reckon/_store.py:323` — `_write_json_envelope`.
- `reckon/capabilities.py:1089` — `rebuild_capabilities`.
- `reckon/crew/dispatch.py:5107` — `_supervisor_write`.
- `reckon/crew/fleet_supervisor.py:180` — `_write_json`.
- `reckon/crew/paid_lanes.py:491` — `write_document_atomically`.
- `reckon/crew/placement.py:118` — `publish_reservation`.
- `reckon/crew/plan_review.py:346` — `store_plan_review`.
- `reckon/crew/resumption.py:175` — `_write_lane_probe_cache`.
- `reckon/crew/review.py:570` — `store_review`.
- `reckon/crew/runs.py:366` — `_write_json`.
- `reckon/resources.py:959` — `migrate_typed_layout`.
- `reckon/serve.py:894` — `_store_git_creation_cache`.
- `reckon/serve.py:1116` — `_store_git_last_modified_cache`.
- `reckon/serve.py:2950` — `Handler._handle_post`.

### 3. One ordered enumeration of original and resumed streams

Three implementations select the original stream and numerically ordered resume files. Reuse prevents billing and promotion from disagreeing about which attempts exist; the metering implementation already handles an empty path. Proposed single home: `reckon/crew/metering.py:run_streams (existing implementation)`.

Constraint: Adapt the ledger's run-id/root input at its boundary; retain numeric resume ordering and missing-file behavior. Do not replace schema-specific stream interpretation with one undocumented parser.

Copies (3 definitions, commit `056c08e0d7c96a5b4edaee5dbcfe169c0c5ceaf9`):

- `reckon/crew/metering.py:280` — `run_streams`.
- `reckon/crew/promotion.py:1694` — `_run_streams`.
- `reckon/ledger.py:2165` — `_run_streams`.

### 4. Share the algebra common to equilibrium constraints

Seven residual methods have identical AST bodies, and five dual-flux-image methods form a second identical group. Their observed-minus-target scaling and Jacobian-axis conventions are algebra that should have one tested implementation. Proposed single home: `nova/equilibrium/constraint.py (shared residual and dual-image implementation)`.

Constraint: Keep each constraint's observed quantity, payload and registration distinct. Preserve JAX tracing and array axis behavior; a common protocol alone does not share implementation.

Copies (12 definitions, commit `16287918db2f90d0088c1786ec45965cd90e6652`):

- `nova/equilibrium/constraint.py:970` — `CurrentCentroidConstraint.residual`.
- `nova/equilibrium/constraint.py:1051` — `FluxLevelConstraint.residual`.
- `nova/equilibrium/constraint.py:1344` — `IsofluxConstraint.residual`.
- `nova/equilibrium/constraint.py:1392` — `XPointConstraint.residual`.
- `nova/equilibrium/constraint.py:1468` — `FieldComponentConstraint.residual`.
- `nova/equilibrium/constraint.py:1530` — `WallGapConstraint.residual`.
- `nova/equilibrium/constraint.py:1645` — `ExternalShafranovConstraint.residual`.
- `nova/equilibrium/constraint.py:1063` — `FluxLevelConstraint.dual_flux_image`.
- `nova/equilibrium/constraint.py:1356` — `IsofluxConstraint.dual_flux_image`.
- `nova/equilibrium/constraint.py:1404` — `XPointConstraint.dual_flux_image`.
- `nova/equilibrium/constraint.py:1480` — `FieldComponentConstraint.dual_flux_image`.
- `nova/equilibrium/constraint.py:1542` — `WallGapConstraint.dual_flux_image`.

### 5. Share axis validation mechanics with explicit precision policy

Three functions independently validate increasing uniform axes. The media readers reconstruct endpoint-preserving axes, while map extraction requires at least three points and strict spacing. Shared mechanics can make those policy choices visible. Proposed single home: `nova/utilities/axes.py:uniform_axis (new common validator)`.

Constraint: Do not force identical tolerances: MAST uses stored dtype epsilon, DIII-D uses an absolute coordinate-scaled tolerance, and map extraction uses relative spacing tolerance. Preserve minimum sizes, return shapes and dtype behavior.

Copies (3 definitions, commit `16287918db2f90d0088c1786ec45965cd90e6652`):

- `nova/equilibrium/map_extraction.py:109` — `_uniform_axis`.
- `nova/media/sources/diiid_efit.py:38` — `_uniform_axis`.
- `nova/media/sources/mast_efit.py:44` — `_uniform_axis`.

### 6. One owned immutable-array constructor

Three helpers independently copy an array and clear its write flag. Sharing the ownership boundary makes accidental aliasing testable in one place. Proposed single home: `nova/utilities/arrays.py:readonly_copy (new common array primitive)`.

Constraint: Retain dtype=None versus dtype=float defaults at the call sites and prove that changing the original input cannot change the returned array. The wider primitive census has additional inlined clients, not all equivalent constructors.

Copies (3 definitions, commit `16287918db2f90d0088c1786ec45965cd90e6652`):

- `nova/imas/mast_efit_referee.py:39` — `_readonly`.
- `nova/transport/coupled_window.py:249` — `_readonly`.
- `nova/transport/window_batch.py:47` — `_readonly`.

### 7. One bool-rejecting numeric observation decoder

Four differently named functions have exactly the same body: reject bool and non-int/float values, otherwise return float(value). This is a low-coupling consolidation with a clear contract. Proposed single home: `reckon/_observations.py:optional_number (new observation primitive)`.

Constraint: Do not silently add finiteness or string coercion; neighboring numeric decoders have different semantics. Both None and non-finite numeric values need explicit expectations.

Copies (4 definitions, commit `056c08e0d7c96a5b4edaee5dbcfe169c0c5ceaf9`):

- `reckon/_backends.py:1608` — `_number`.
- `reckon/crew/carryover_census.py:215` — `_measured`.
- `reckon/crew/hold.py:178` — `_as_number`.
- `reckon/crew/window_reading.py:266` — `_numeric`.

### 8. One atomic JSON writer for nova's durable receipts

Three writers independently stage and replace chunk, fingerprint and scorecard files. Shared mechanics can give each a unique sibling temporary and consistent cleanup while retaining its payload. Proposed single home: `nova/io/json.py:write_atomic (new shared writer)`.

Constraint: Preserve strict JSON sanitization, indentation and digest-sensitive serialization. These are file receipts, not IMAS data access. A dataclass replace call was explicitly excluded by the instrument control.

Copies (3 definitions, commit `16287918db2f90d0088c1786ec45965cd90e6652`):

- `nova/calibrate/sweep.py:372` — `write_chunk`.
- `nova/catalog/mast_geometry.py:175` — `_write_fingerprint_checkpoint`.
- `nova/imas/mast_parity_gate.py:575` — `_bank_report`.

### 9. Extend the already shared contract validation with trimmed text

Three helpers repeat non-empty, whitespace-trimmed text checks after calling the same require_string primitive. The existing cross-module dependency provides a natural home without a new abstraction family. Proposed single home: `nova/imas/machine_evidence.py:require_trimmed_string (alongside require_string)`.

Constraint: Accept the caller's exception type and context so SourceMapError, DriveError and EvidenceError retain their current meaning.

Copies (3 definitions, commit `16287918db2f90d0088c1786ec45965cd90e6652`):

- `nova/imas/machine_drive.py:79` — `_trimmed`.
- `nova/imas/machine_evidence.py:114` — `_trimmed`.
- `nova/io/sourcemap.py:79` — `_trimmed`.

### 10. One digest operation over canonical contract bytes

Three content-addressed contracts hash canonical_bytes with SHA-256 and truncate to 16 hex characters. The shared canonical JSON layer is the appropriate home for the byte-level digest convention. Proposed single home: `nova/imas/machine_evidence.py:canonical_digest (beside canonical_json)`.

Constraint: Pass the existing canonical bytes unchanged and retain the 16-character length. Do not substitute catalog serialization just because it also hashes JSON; byte identity is the contract.

Copies (3 definitions, commit `16287918db2f90d0088c1786ec45965cd90e6652`):

- `nova/imas/machine_drive.py:323` — `DriveMap.digest`.
- `nova/imas/machine_evidence.py:491` — `EvidenceLedger.digest`.
- `nova/io/sourcemap.py:499` — `SourceSignalMap.digest`.

Repeated `main`, `resolve`, or `fit` names are not a consolidation mandate. Likewise, five identical config-home bodies in standalone hooks are retained as candidates in the raw census but ranked below this list: removing their bootstrap independence needs its own design justification. Calling `np.diff`, `np.interp` or a digest primitive from multiple scientific operations is usually reuse of a library, not proof of reimplementing a helper.

## Reproduction and evidence limits

The compact repository JSON retains report figures and ranked copies. Histograms, all module and function locations, complete helper groups and raw clone windows live only in the full census at `/home/ITER/mcintos/.config/reckon/crew/runs/r-20260926T104810100005-code-depth-reuse-census/code-depth-full.json`. The compact file records that artifact's byte size and SHA-256.

Run from this repository with its root environment: `UV_PROJECT_ENVIRONMENT=/home/ITER/mcintos/Code/reckon/.venv uv run --no-sync python docs/research/data/crew-pattern-review/code-depth/census.py --check`. The default sibling repository root is `/home/ITER/mcintos/Code`; override it with `--repo-root`. The checked-in pins are inputs, so a growing branch cannot change this result. Python must understand the snapshots' syntax; the recorded interpreter is Python 3.14.2. No environment synchronization, package import, test execution, external API or model request is needed.

`census.log` records the complete measurement and the full-output path; `reproduction.log` records the second run and byte-identity receipt. Controls exercise a known twelve-line duplicate, a single-copy non-match, forwarding versus transformation, CLI/options/MCP/refusal extraction, and file replacement versus dataclass replacement. `render.py` reproduces this report, the ranked candidate JSON and the trend figure from `code-depth.json`.

This is a static structural assessment. It does not measure call-site reuse, runtime exports, branch complexity, test quality, performance, scientific correctness or worker-lane causation. No production source changed. Each proposed consolidation needs a separately scoped implementation and behavior-preservation gate; the coordinator owns that sequencing and the combined review.
