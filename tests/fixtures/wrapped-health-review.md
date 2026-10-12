# Health review — obligations snapshot / coordinator hook / watch unit

Reviewed revision: 0f57ee7b23a1d4bb2592c5fb82d92f3dec2104b5
Neighbourhood: `reckon/crew/obligation_snapshot.py`, `reckon/hooks/coordinator_obligations.py`,
`reckon/crew/watch_unit.py`, and the import-adjacent modules listed in the brief.

## Evidence gathered

- Clone detector (`reckon.clones.clone_matches`, `WINDOW_LINES=6`) over the three named
  modules: 6 matches — `_config_home` across two modules, and the `_inject_list`/
  `_prompt`/`_stop` trio sharing windows inside `coordinator_obligations.py`.
- Token sizes (`reckon.crew.routing._tokens_for_bytes`) against the 50,000-token
  worker-window bound (`tests/test_every_module_fits_a_worker_window.py:25`): the
  largest neighbourhood module is `coordinator_obligations.py` at 14,212; the largest
  one-import-away module is `runs.py` at 34,246 — all under bound.
- Entry points: `obligation_snapshot` is imported by `crew/session_host.py`,
  `crew/runs.py`, `crew/budget_reset.py` and `hooks/coordinator_obligations.py`;
  `coordinator_obligations` by `hooks/install.py` and `mcp_census.py`; `watch_unit` by
  `mcp_census.py` — all reached, see below.
- The named path `tests/test_a_remote_producer_reads_as_live.py` **does not exist** and
  appears in no git revision (`git log --all -- <path>` empty).

## Rubric

RUBRIC known_findings: a finding — the brief's rows fall into five classes: writer/reader
parity drift (`obligation_snapshot.py:487`, `:560` x2); accepted second copies of one
capability (`:561`, `:324`, and the duplicated rationale comment); dead/never-referenced
symbols (`:178`, `coordinator_obligations.py:625`); the tier of a plan section re-deriving
a fact an owner already computes (`:809`, `:607`, `watch_unit.py:60`, `coordinator` — see
FINDINGS); and two live hook-behaviour defects (`coordinator_obligations.py:294`, `:528`).
The parity-drift and second-copy classes recur across several rows; each is cited below,
not re-reported.

RUBRIC duplicate_capability: a finding — `_config_home` is implemented twice, at
`reckon/crew/obligation_snapshot.py:107` and `reckon/hooks/coordinator_obligations.py:242`
(clone detector, window line 242); the oldest-age derivation exists twice, at
`reckon/crew/obligation_snapshot.py:829` and `reckon/crew/obligations.py:900`; and the
foreign-host producer test `reckon/crew/watch_unit.py:60` overlays the local/remote
freshness distinction `reckon/crew/obligation_snapshot.py:354` also computes.

RUBRIC unreached: pass — all three named modules are reached at runtime:
`obligation_snapshot` by `crew/session_host.py`, `crew/runs.py`, `crew/budget_reset.py`
and `hooks/coordinator_obligations.py`; `coordinator_obligations` by `hooks/install.py`
and `mcp_census.py`; `watch_unit` by `mcp_census.py`. The dead public `resolve()`
recorded in the brief is already gone at this revision (no `def resolve(` in
`coordinator_obligations.py`), so no named module is unreached; only the named **test**
path is absent — see FINDINGS. `_process_is_running` is reached, at
`obligation_snapshot.py:296`, so the brief's `:178` claim that it is never called is
stale at this revision, and `_NOT_FRESH_REASONS` no longer exists in the module.

RUBRIC interface_growth: a finding — the last 14 days added a large public surface:
`freshness`, `live_payload`, `payload_for`, `payload_measured_from`, `snapshot_dir`,
`snapshot_path`, `read_snapshot`, `write_snapshot`, `source_code_stamp`,
`reload_in_progress`, `recheck_worktree_held_rows`, `producer_lease_is_live`,
`producer_lease_path`, `watch_host_lease`, `watch_state`, `snapshot_module` and others
(`git log --since='14 days ago' -p -- <three modules>`). The only retirements in the same
window are `resolve` (duty view) and the `crew review-plan` alias (`18739912f`), a small
fraction of the addition.

RUBRIC width: pass — no neighbourhood or import-adjacent module exceeds the 50,000-token
worker-window bound; the three named modules measure 10,768, 14,212 and 10,184 tokens.

RUBRIC test_debt: a finding — `_producer()` is duplicated at
`tests/test_skipped_record_reaches_the_checklist.py:81` and
`tests/test_a_worktree_held_row_is_rechecked_at_read.py:165`; `_live_run(` is duplicated at
`tests/test_obligations_hook_reads_the_snapshot.py:122` and
`tests/test_coordinator_obligations_hook.py:105`; `_drive(` is duplicated at
`tests/test_obligations_hook_reads_the_snapshot.py:217` and
`tests/test_a_worktree_held_row_is_rechecked_at_read.py:223`. Separately, the test the
neighbourhood most needs — a remote/live producer test — is named in the brief but absent.

## Findings

FINDING duplicate_capability reckon/crew/obligation_snapshot.py:107 — CLASS:
duplicated_capability — `_config_home` is re-implemented at
`reckon/hooks/coordinator_obligations.py:242` (docstring at :242 states the copy is
intentional to avoid importing the store on the prompt path), so the two can drift — CITES:
new (cited by the clone detector; the brief carries the sibling `:324` sibling-set row but
not this pair)

FINDING duplicate_capability reckon/crew/obligation_snapshot.py:829 — CLASS:
duplicated_capability — the oldest-age derivation `oldest_age_seconds` is computed here and
again at `reckon/crew/obligations.py:900`, so a change to one leaves the other wrong —
CITES: `obligation_snapshot.py:809 [reuse_search]`

FINDING duplicate_capability reckon/crew/obligation_snapshot.py:354 — CLASS:
duplicated_capability — `freshness` re-derives the local-vs-foreign-host producer
distinction already owned by `reckon/crew/watch_unit.py:60` (`_seat_names_a_foreign_host`),
so the same question has two owners — CITES: `watch_unit.py:60 [duplicate_owner]`

FINDING unreached tests/test_a_remote_producer_reads_as_live.py:1 — CLASS:
missing_named_path — the brief names this test file and it exists in no revision
(`git log --all -- <path>` empty); the remote-producer behaviour it would guard is instead
covered only indirectly by `tests/test_watch_observer_is_host_aware.py`, so the §5
remote-vs-local producer claim has no direct test — CITES: new

FINDING test_debt tests/test_skipped_record_reaches_the_checklist.py:81 — CLASS:
duplicated_helper — `_producer()` is copied verbatim at
`tests/test_a_worktree_held_row_is_rechecked_at_read.py:165`; a change to the producer
fixture shape must be made in two places — CITES: new

FINDING test_debt tests/test_obligations_hook_reads_the_snapshot.py:217 — CLASS:
duplicated_helper — `_drive(` is copied at
`tests/test_a_worktree_held_row_is_rechecked_at_read.py:223` (and `_live_run(` at
`test_coordinator_obligations_hook.py:105` duplicates
`test_obligations_hook_reads_the_snapshot.py:122`), so the hook-driving scaffold has three
copies — CITES: new

FINDING known_findings reckon/hooks/coordinator_obligations.py:294 — CLASS:
collapsed_distinct_items — the collapse fires whenever two obligations share a kind and a
next command, so two review-ready items render as one counted line carrying neither run id
— CITES: `coordinator_obligations.py:294 [finding]`

FINDING known_findings reckon/hooks/coordinator_obligations.py:528 — CLASS:
stale_digest_hides_reappearance — the stored digest is not cleared when the duty view is
empty, so an identical re-appearance compares equal to the stale digest and the hook stays
silent (measured in `reviewer-probe-reappearance.log`) — CITES:
`coordinator_obligations.py:528 [finding]`

## Treatment

TREATMENT — GOAL: make the obligations derivation the single owner of the unreadable-row
build, deleting the ad-hoc rebuild in `live_payload` — WRITE_PATHS:
reckon/crew/obligation_snapshot.py, reckon/crew/obligations.py — DONE_WHEN:
`live_payload` no longer constructs unreadable rows itself and a parity test feeds a
truncated record through both the derivation (`obligations.obligations()`) and
`live_payload` and asserts the two row lists are equal; negative control: at the base
revision the same test is green — i.e. the parity only holds because a fixture with no
finding compares equal — so a fixture that plants a truncated record reds the test until
the derivation owns the build (`tests/test_obligations_snapshot_is_published.py:229` and
`:423` today carry no finding) — EXPECTED_DELTA: -18