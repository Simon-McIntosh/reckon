# Orphaned watcher replacement: diagnosis

Observed on 2026-09-21 at assigned revision `26d6df25680ac385446f9db0c86f339ddb57fece`.
All current source conclusions, including those about `reckon/crew/runs.py`, refer to that revision. No production or test file was changed.

**Verdict: the case has an invalid dispatch fixture; the observed red does not identify a replacement-path defect.** Its `_node()` omits `spec_level`, so validation refuses the node before watcher arming. Supplying only `spec_level="exact"` in memory makes the existing real-process replacement test pass, including replacement identity, supervisor liveness, recorded watcher liveness and cleanup. The replacement assertions remain appropriate; the fixture's assumption that an undeclared specification is dispatchable is obsolete.

## Observed revision boundary

The change introducing this deterministic failure is `d5811b204966f31db83bab190b6440a6eb7b9842`, committed 2026-09-04 at 12:36:05 +0200. The first mainline integration of that change is `8041fd4e5deeb0fe9b0b23503f7c1ab4a038b4f2`, committed at 13:26:45 +0200 the same day.

This is an experimentally established parent/change boundary, not an attribution from commit titles. `git log -S 'no specification level is declared' -- reckon` located the validation change; its diff adds the missing-specification finding in `reckon/crew/node.py`. Then four archived source snapshots were executed with the root project's Python environment, each running `python -m pytest -p no:cacheprovider -q tests/test_crew_orphan.py` on SLURM `all_debug`. Snapshots were temporary data extracted with `git archive`, removed after each run; no checkout, branch switch or git bisect was used. Each run's configuration and fixture repository were temporary. Historical watcher teardown targeted only the isolated test processes.

| Observed revision | Relationship | Result | Exit |
| --- | --- | --- | --- |
| `319db8de5cd81cf9c95e1b80e8b2b894c6e884cd` | Introducing change's parent | `2 passed in 1.03s` | 0 |
| `d5811b204966f31db83bab190b6440a6eb7b9842` | Specification declaration becomes required | `1 failed, 1 passed in 0.49s` | 1 |
| `d312c5bc92cbfaf08ae20ee02f2b3a4c19d4bfe7` | Mainline merge's first parent | `2 passed in 0.92s` | 0 |
| `8041fd4e5deeb0fe9b0b23503f7c1ab4a038b4f2` | Merge into main | `1 failed, 1 passed in 0.42s` | 1 |
| `26d6df25680ac385446f9db0c86f339ddb57fece` | Assigned current base | `1 failed, 1 passed in 7.23s` | 1 |
| Same assigned base, diagnostic fixture field supplied in memory | Existing replacement case alone | `1 passed in 23.68s` | 0 |

The historical test blob is identical in all four snapshots: `8b0857dabc494cd5aa5583fadfc0ad736e4a62d3`. The dispatch module blob is also identical across them: `218f10e4f455fa29619cb2d54d86fcd8a573c35b`. Those identities rule out silently changing the case or replacement implementation between the paired historical measurements. The current test differs from its historical version in the later safe-signalling change; the same specification omission remains.

**Inference and limit:** this establishes where the present persistent failure was introduced and entered main. It is not a census proving that the process-dependent test never suffered a transient failure before that boundary. No earlier CI history was asserted or reconstructed.

## Exact failure, and the requested assertion values

**Observed:** there is no failing assertion or actual/expected assertion pair in this reproduction. Pytest reports an uncaught `CrewError` at the call to `crew_dispatch.dispatch`, before the replacement assertions. Inventing an `assert False is True` excerpt would misreport the run. The historical failure line, verbatim from both red historical logs, is:

```text
E           reckon.crew.CrewError: node is not dispatchable — spec-level: no specification level is declared; pass --spec-level exact, guided or open so the run remains visible to calibration
```

The current failure line, verbatim from `current-orphan.log`, is:

```text
E           reckon.crew.CrewError: node is not dispatchable — spec-level: no specification level is declared; pass --spec-level exact, guided or open so the run remains visible to calibration Resolve with `reckon crew dispatch` after repairing the named node-contract property.
```

The actual fixture value and the validator's accepted control were measured directly, without spawning a worker or mutating the fixture file. These are verbatim lines from `fixture-contract.log`:

```text
OBSERVED original fixture spec_level: ''
OBSERVED original validation: NodeValidation(ok=False, findings=[{'property': 'spec-level', 'detail': 'no specification level is declared; pass --spec-level exact, guided or open so the run remains visible to calibration'}])
DIAGNOSTIC supplied spec_level: 'exact'
OBSERVED supplied validation: NodeValidation(ok=True, findings=[])
```

Thus the observed actual input is `''`; the refusal explicitly asks for `exact`, `guided` or `open`. These are input-contract values, **not** values extracted from a nonexistent failing assertion. The requested assertion-pair wording in the done-when has a false premise; the complete observed exception and measured input values above are the applicable evidence.

## Why this distinguishes the fixture from the path

**Observed source:** `_node()` in `tests/test_crew_orphan.py` constructs a `TaskNode` without `spec_level`. `TaskNode` defaults that field to `""`. `validate_node()` rejects it, and `dispatch()` raises on the failed validation before reaching `_ensure_watch_producer()`. The originally observed failure therefore cannot measure replacement.

**Observed positive control:** a pytest collection plugin wrapped only this module's `_node()` in memory, called the original factory, set `node.spec_level = "exact"`, and returned it. It did not patch watcher liveness, the launcher of the watcher, the replacement function, follower delivery, or any assertion. The worker-launch stub already present in the test was retained. The existing replacement case passed. The log reads:

```text
DIAGNOSTIC ONLY: fixture spec_level supplied as exact; repository files unchanged
.
1 passed in 23.68s
```

That execution first established a real held orphan seat with `observer_alive is False`, then passed the original assertions that the replacement seat is held, its PID differs from the orphan's PID, its observer is alive, and `record["watch"]["watcher_live"] is True`. It also passed the original assertions that unwatch stops the replacement, releases the registration, and leaves `{}` in the lock record. The living-parent sibling passed in the unmodified baseline as a separate positive control for the liveness instrument.

**Observed source:** `_ensure_watch_producer()` still replaces a live orphan via `unwatch()` and `_start_watch_producer()`. The later commit `5ec236d108bf727735d0a214a74bcbad9f34d1c0` does not remove that behavior: it makes admission refuse when the resulting state has no live watcher, and names `watch --ensure` in the refusal. A delivering follower and a live watcher remain separate requirements.

**Inference:** the smallest justified repair is in `tests/test_crew_orphan.py`: give `_node()` an explicit valid `spec_level`, retaining its real-process assertions and teardown. `exact` was measured successfully. Do not weaken the production validation or reinterpret the case to accept a missing replacement. The assertion the test intended—an orphan is replaced, not merely reused or refused—still holds for a valid node at the observed revision. The assumption that no specification need be declared is what stopped being right.

## Follow-on and limits

The fixture repair is outside this report-only scope and is not applied here. A repair node should update that one test file, rerun its two cases, and demonstrate that bypassing orphan replacement reddens the replacement case. That last mutation has not been run by this diagnostic node; no test deliverable is claimed.

The plan's observations of inactive systemd watcher units are separate evidence. This test starts a detached supervised producer; it does not test systemd restart policy or a lost user bus. Its success cannot establish that those operational problems are fixed, and its current failure cannot explain them. No live service was restarted or altered. The separate unit-restart and service-manager-fallback work remains necessary on its own evidence.

For an actual failed replacement, current admission should give a `WatcherRequired` refusal naming `reckon crew watch --project <project> --ensure`, rather than admit a run with no live watcher. That is a source-derived expectation, not an observed failure in this case, and no production repair is justified by this measurement.

## Durable evidence and reproduction

All logs are under the delivered run directory:

`/home/ITER/mcintos/.config/reckon/crew/runs/r-20260921T123905355879-orphaned-watcher-replacement-diagnosis/`

- `current-orphan.log`: unmodified assigned-base module run.
- `declared-spec-probe.log`: in-memory fixture control and unchanged replacement assertions.
- `fixture-contract.log`: actual omitted field, observed refusal, and accepted field control.
- `d5811b20-parent.log`, `d5811b20.log`: introducing-change pair.
- `8041fd4e-parent1.log`, `8041fd4e.log`: mainline integration pair.
- `history_probe.py`, `history-driver.log`, `history-results.json`, `history-blobs.json`: executable snapshot procedure, allocation output, exit receipts and source identities.

The historical driver is rerunnable with `srun --partition=all_debug --time=00:10:00 --cpus-per-task=1 --mem=2G env TMPDIR=/tmp /home/ITER/mcintos/Code/reckon/.venv/bin/python <run-directory>/history_probe.py`. The allocated historical runs completed; the driver's initial TMPDIR warning was followed by SLURM selecting `/tmp`, and all four receipts were recorded. No full-suite or merged-head verification was requested or performed.
