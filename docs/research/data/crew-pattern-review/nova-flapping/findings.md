# Nova history: localized reversals, no demonstrated broad rise under the local lane

**Verdict.** Nova's history contains real backtracking, a weakened then restored test guard, and a contract that changed back. The evidence does **not** establish that flapping broadly increased during the local-lane period, or that the local lane caused readiness to fall. Across additions with a complete seven-day follow-up, source churn changes from **5,474/59,037 = 9.27% before September 12** to **694/7,390 = 9.39% afterward** (0.12 percentage points). Test churn changes from **1,784/63,880 = 2.79%** to **276/8,916 = 3.10%** (0.30 points). Those small aggregate differences coexist with a large change in the lane mix: local contributions rise from **37/481 = 7.69%** to **99/152 = 65.13%** of code landings.

The boundary matters. Local code first landed on **September 5 at 08:46:49 UTC**. Splitting there instead gives source churn **4,564/42,035 = 10.86% → 1,604/24,392 = 6.58%**, and test churn **1,426/47,287 = 3.02% → 634/25,509 = 2.49%**. A broad rise is therefore not robust to the period definition. These are observational comparisons of different tasks and code, not an experiment on lane quality. Readiness is owned by the separate readiness study; this node ran no solver suite and makes no readiness-pass claim.

## Scope and reproducibility

The fixed census covers **2026-08-15 00:00 UTC through 2026-09-26 10:00 UTC**, with Nova `main` pinned at `16287918db2f90d0088c1786ec45965cd90e6652` and the preceding baseline at `e9476d94024ec5ebfb43af785c568d58aa64b3ee`. It contains **3,879 first-parent landings**, of which **633** change `nova/` or `tests/`. Source receives **69,343 additions / 11,044 deletions**; tests receive **83,228 / 3,374**. Merge changes are compared with their first parent, so a worker change and its merge do not count twice.

[census.py](census.py) reads only pinned Git objects, including the committed 1,768-run ledger. [flapping.json](flapping.json) is the 60,627-byte compact record: it preserves the headline denominators, per-project, per-lane and per-week tables, recurrent-file/function coverage, and five named episode summaries. The 4,165,888-byte [full census](/home/ITER/mcintos/.config/reckon/crew/runs/r-20260926T104924535917-nova-flapping-history/flapping.json) preserves every first-parent commit, attribution and short-lived-line edge outside the repository. [census.log](census.log) records a complete run and byte-identical reproduction. The full JSON SHA-256 is `3ef015d8e200a07ad11044509a41368a4337f611b39dcf26c10ac5dbbe47a3a0`.

The actual census command uses the existing Reckon Python 3.14 environment, without syncing or importing Nova:

```bash
/home/ITER/mcintos/Code/reckon/.venv/bin/python docs/research/data/crew-pattern-review/nova-flapping/census.py
```

Run this from the assigned Reckon checkout. The default full output is `/home/ITER/mcintos/.config/reckon/crew/runs/r-20260926T104924535917-nova-flapping-history/flapping.json`; the compact output is `flapping.json` beside the script. `--output` permits a separate full reproduction file outside the repository, and `--compact-output` selects a separate compact output. Both outputs reproduce byte for byte; the compact SHA-256 is `dafc20311f457f9bea25652626497f1c092ab702978d582598cc5f5e5d07891c`. The compact projection reads the selected episode evidence retained in the same run directory. [review.py](review.py) regenerates [episode-evidence.json](/home/ITER/mcintos/.config/reckon/crew/runs/r-20260926T104924535917-nova-flapping-history/episode-evidence.json) and [episode-diffs.patch](/home/ITER/mcintos/.config/reckon/crew/runs/r-20260926T104924535917-nova-flapping-history/episode-diffs.patch) from the five selections in [episode-inputs.json](episode-inputs.json). Its two runs also produced identical bytes, recorded in [review.log](review.log). [render.py](render.py) produces the [weekly figure](../../../../figures/orchestrator-crew-pattern-studies/nova-churn.svg).

## Weekly measured history

A line is tagged at its first-parent addition and counted once if a later first-parent diff deletes it within 604,800 seconds. Blank lines, comments and moves count: this is physical-line churn, not a semantic defect rate. Mature shares restrict **both numerator and denominator** to additions at least seven days old at capture. The September 19 row's mature cohort includes only its first ten hours; September 26 is the final ten-hour partial bin and has no commits. The JSON also preserves observed lower-bound shares for all additions.

| Week beginning UTC | All / code landings | Source + / − | Tests + / − | Source deleted / mature additions | Tests deleted / mature additions | Explicit reverts: first-parent / reachable |
|---|---:|---:|---:|---:|---:|---:|
| 2026-08-15 | 465 / 118 | 19,622 / 4,371 | 19,910 / 982 | 2,653/19,622 (13.52%) | 892/19,910 (4.48%) | 0 / 0 |
| 2026-08-22 | 878 / 174 | 14,369 / 1,805 | 17,313 / 796 | 1,199/14,369 (8.34%) | 349/17,313 (2.02%) | 0 / 0 |
| 2026-08-29 | 464 / 60 | 6,025 / 689 | 7,638 / 212 | 489/6,025 (8.12%) | 157/7,638 (2.06%) | 0 / 0 |
| 2026-09-05 | 684 / 129 | 19,021 / 2,181 | 19,019 / 640 | 1,133/19,021 (5.96%) | 386/19,019 (2.03%) | 0 / 6 |
| 2026-09-12 | 516 / 57 | 5,992 / 741 | 7,581 / 135 | 515/5,992 (8.59%) | 159/7,581 (2.10%) | 0 / 0 |
| 2026-09-19 | 872 / 95 | 4,314 / 1,257 | 11,767 / 609 | 179/1,398 (12.80%) | 117/1,335 (8.76%) | 0 / 0 |
| 2026-09-26 | 0 / 0 | 0 / 0 | 0 / 0 | 0/0 (not estimable) | 0/0 (not estimable) | 0 / 0 |

## Attribution and repeated editing

| Attributed lane | Code landings | Source deleted / mature additions | Tests deleted / mature additions |
|---|---:|---:|---:|
| claude | 15 | 372/2,519 (14.77%) | 126/2,689 (4.69%) |
| codex | 445 | 5,222/53,662 (9.73%) | 1,555/56,484 (2.75%) |
| local | 136 | 428/4,861 (8.80%) | 275/10,873 (2.53%) |
| mixed | 3 | 5/1,247 (0.40%) | 6/1,203 (0.50%) |
| unattributed | 34 | 141/4,138 (3.41%) | 98/1,547 (6.33%) |

**34/633 = 5.37% of code landings remain unattributed; another 3/633 = 0.47% have mixed lane ownership.** Across all first-parent commits, 2,434/3,879 = 62.75% are unattributed, predominantly record and coordinator commits; no author-name or timing guess fills those gaps. A merge's lane denotes the ledger-linked contributions it brought in, not the integrator's runtime. The pinned ledger has **178 commit citations that are not uniquely reachable at the capture**; they remain listed, without being treated as missing implementation or forced matches. For five branch reverts absent from explicit commit lists, attribution is separately marked as membership in a recorded run's base-to-listed-tip ancestry, with both bounds retained.

**43 files and 95 qualified functions/methods change in at least three distinct weeks.** `fixed_point.py`, `forward.py`, `forward_operator.py` and `topology.py` change in all six populated weeks. `ForwardFluxOperator.__post_init__` changes in six; `ForwardProfile._solve_accelerated`, `ForwardFluxOperator._fixed_design_read` and `_support_partition` change in five. Frequent change is a hotspot signal, not itself oscillation; nested functions also contribute to their enclosing function's AST hash.

The exact-return instrument tracks 6,099 constant keys, 3,390 defaults, 14,831 signatures, 11,927 individual expectations and 4,599 assertion sets. It observes 201, 10, 447, 86 and 214 value changes in those categories, respectively. It finds **two returned expectations in one first-parent episode**, both in playable program reuse below. There are **zero first-parent explicit revert commits**, but **six explicit reverts in merged ancestry**: five belong to one compiled-cache run and one restores the certificate guard. All six first reach `main` in the week beginning September 5. Counting only the first-parent commit subjects would miss them.

## Five most consequential flapping and rewrite histories

These five histories were selected from direct returns, every explicit revert, and the largest short-lived source-line edges. The ordering prioritizes exact restoration, guard weakening and discarded work; it is an engineering judgment, not a fitted severity score. The classification on each history matters: there are not five independent, automatically detected value-return episodes. Full diffs and per-snapshot marker observations are retained so another reader can challenge each interpretation.

### 1. Compiled-slice experiments return the whole solver file to an earlier state

`nova/equilibrium/reduced_newton.py` at [e04983134337](https://github.com/Simon-McIntosh/nova/commit/e04983134337ed1c41ae0bc28827a119cac8f180) introduced compiled executable reuse. The cache key then moved ahead of coordinate reconstruction at [a689116db325](https://github.com/Simon-McIntosh/nova/commit/a689116db325ffbe0188443e9fee07f5d279c190); adaptive refresh, grade ordering, trip snapshots and reconstruction boundaries followed. Five commits then withdrew those experiments:

| Withdrawal | Reverted mechanism |
|---|---|
| [c6f42c0018a8](https://github.com/Simon-McIntosh/nova/commit/c6f42c0018a894175f657463143518dbc194a374) | Restored coordinates and the derived external field as cache identity, replacing the conductor-input/state-shape key. |
| [faec266dedfc](https://github.com/Simon-McIntosh/nova/commit/faec266dedfc33f2d593395592eaa8bfafcb16d4) | Removed the reconstruction barrier and nested compiled reconstruction experiments. |
| [a3444bf77f7a](https://github.com/Simon-McIntosh/nova/commit/a3444bf77f7a001950357152208f2ed93a91e3c5) | Restored the compiled grade program and withdrew its ordering/cast experiment. |
| [3a9ce7f80d93](https://github.com/Simon-McIntosh/nova/commit/3a9ce7f80d938738b81475cad4045d026808280e) | Removed adaptive refresh from the compiled solve. |
| [c636a4ee2016](https://github.com/Simon-McIntosh/nova/commit/c636a4ee20164448b2e409ab885cf236c8485d73) | Removed trip-state capture from signatures, carry and result fields. |

The last file is **byte-identical to the initial 116,212-byte file**, SHA-256 `ab97e592458c4a788f38576757970f715332114cac5c30e54fbe69641f98499c`. This is a checked equality, not acceptance of a revert message. All swings are bounded by the ledger record for **Codex Terra (`codex-terra`), `millisecond-converged-solve`, run `r-20260909T104503283227-mcs-compiled-slice-cache`**. The five withdrawals are ancestry-linked between its recorded base and listed tip `36a1ede51f240eb9d55a5e094ba9f3e954350c32`; they are not individually listed ledger commits. They first reach the primary branch together in [1176ad0fd35d](https://github.com/Simon-McIntosh/nova/commit/1176ad0fd35db3ad3329413aff9669cd4f30c99d). **Classification: substantial worker backtracking caught before merge; not five separate primary-branch regressions and not a local-lane episode.**

### 2. A certificate guard becomes permissive, then strict again

In `tests/test_certificate_pin.py`, [ee81907552b2](https://github.com/Simon-McIntosh/nova/commit/ee81907552b21859048a35b0dffbf4050ed7e01f) requires reading each figure and matching its SHA-256. It belongs to **Codex Terra, `gs-absolute-accuracy-gates`, run `r-20260909T160346985581-gaa-certificate-artifact`**. Then [d2a1382b0609](https://github.com/Simon-McIntosh/nova/commit/d2a1382b0609ee2f77c6dea20899f2504335a747) wraps that check in `if figure_path.exists()` and accepts a missing file when its recorded digest merely has length 64. That swing belongs to **Codex Luna (`codex-luna`), `null-identification-authority`, run `r-20260909T185031896287-nia-joint-qualification-rule`**. Finally [b4cb7f8889ff](https://github.com/Simon-McIntosh/nova/commit/b4cb7f8889ff3a4636f91b73496c32a6b2b5d76e) restores the unconditional file read and digest comparison, while allowing both documented render provenance values. The repair belongs to **local (`clive`), `null-identification-authority`, run `r-20260909T231050433481-nia-certificate-figure-rerender`**.

**Classification: a real guard-policy reversal on landed history, repaired by the local lane after a metered-lane relaxation.** Its final full assertion set differs in render provenance, so the narrow exact-set detector does not label the whole function a returned value; reading the guarded condition finds the partial reversal. The reviewed source establishes the code change; this history study did not execute the certificate test or infer a current test verdict.

### 3. Playable program reuse changes `True → False → True`

`tests/test_playable_session.py` receives `keyframe.reused is True` and `settled.reused is True` in [2db98d261c23](https://github.com/Simon-McIntosh/nova/commit/2db98d261c233e848c77bb9097b33c1c56d3b1d5) (**local, `playable-forward-solve`, run `r-20260906T023506258562-pfs-app-reduced-route`**). Both change to `False` at [b8f06271d945](https://github.com/Simon-McIntosh/nova/commit/b8f06271d9453913e5d75caf1d555d0971739eea) (**Codex, the same plan, run `r-20260906T104326588527-pfs-inverse-step-shape-control-sol-2`**) when inverse control changes prescribed currents and each key builds a program. They return to `True` at [95788d466716](https://github.com/Simon-McIntosh/nova/commit/95788d46671618edced85ddf1cee1a48b5d16f41) (**Codex, the same plan, run `r-20260915T133936592157-pfs-playable-keyframe-fence-from-measurement`**) when currents become traced inputs and the program is carried across keys.

**Classification: the one exact primary-branch value-return episode, with two expectation keys.** The final test also checks program object identity and adopts a measured CPU timing bound, so this is not an unchanged implementation endlessly toggling expectations. The screened episode rate is 0/481 code landings before September 12 and 1/152 afterward (0.66 per hundred), too few observations to infer a general lane effect. Its return occurs during the study period but is Codex-attributed; the originating local value was followed by two metered-lane swings.

### 4. A request dispatcher is added, then removed after about 44 hours

[39b8bedb4451](https://github.com/Simon-McIntosh/nova/commit/39b8bedb445131593248b97fc9a3530e5b0505ae) adds `FluxReadRequest`, `FluxReadAnswer`, `dispatch_read_requests` and `read_with_current_moments` to `nova/equilibrium/forward_operator.py`. [be5754d82ba9](https://github.com/Simon-McIntosh/nova/commit/be5754d82ba96ba6af46d4950701e023c5f0bffb) deletes the request enum, answer carrier and dispatcher, while **retaining `read_with_current_moments`**. The census tracks **97 lines** added by the first landing and removed by the second within seven days; the file's first change was +112/−0, its withdrawal +0/−97. Both swings are **local (`clive`), `millisecond-converged-solve`**, in runs `r-20260917T170314445551-msc-request-set-machine-2` and `r-20260919T223218420452-msc-inert-dispatcher-and-fused-branch` respectively.

**Classification: a genuine landed addition/withdrawal and wasted implementation surface, with the useful fused read retained.** It is not an `A → B → A` scalar value cycle and does not by itself show a solver-readiness regression. This is the clearest local-only rework episode among the five.

### 5. Transport geometry prototypes are rewritten, restored to a legacy route, then retired

In `nova/transport/current_diffusion.py`, [887f3fd79419](https://github.com/Simon-McIntosh/nova/commit/887f3fd7941998fdce1f9bd6e68c323bc56c8448) adds exact clipped-cell surface moments; [378b24df1128](https://github.com/Simon-McIntosh/nova/commit/378b24df1128ec5e0c4e956c2c05ebffa980fdfa) replaces their path with tensor-bicubic work; [9fb3dc699d75](https://github.com/Simon-McIntosh/nova/commit/9fb3dc699d75e117900bb2b64bb187a56c8c71a4) grows the monotone-arc machinery while switching legacy cumulative/extrema calls to `_single_arc_moment_correction`; and [2a80b890bf3c](https://github.com/Simon-McIntosh/nova/commit/2a80b890bf3cd869ae96123c0b1265b276e794bb) retires the in-file transport kernels. Every swing is **Codex, `flux-function-forward-transport`**; the exact contributing runs and worker commits for each merge are in [episode-evidence.json](/home/ITER/mcintos/.config/reckon/crew/runs/r-20260926T104924535917-nova-flapping-history/episode-evidence.json).

This file has **1,327 added lines deleted within seven days** over the census, the largest file total. Of those, **409** from the monotone-arc/legacy-route landing, **300** from the tensor-bicubic landing and **241** from the exact clipped-cell landing are deleted in the retirement merge. The final retirement diff is +11/−1,518. **Classification: heavy prototype churn and a route backtrack before kernel consolidation, preceding any local Nova code landing.** Treating every retired prototype line as regression would make the early Codex period look worst while discarding the reason for consolidation.

## Instrument checks and limits

The detector's synthetic controls produce returns in every measured category and reject monotonic changes and disappear/reappear sequences. Real history supplies 958 measured value changes across the five categories, so the small return count is not an empty parser result. The replayed final contents match **1,004 Git text blobs**, independent `git log --numstat` with matching zero-context/no-rename options matches all four gross line totals, and **zero AST parse failures** remain under Python 3.14. One binary-file change and 668 ambiguous feature/snapshot keys are explicitly excluded. Initial measurements made with the system Python and mismatched Git diff options are retained in the run directory as instrument-development logs; they are not the published result.

The exact AST screen ignores formatting, tracks stable file/function/key identities and refuses ambiguous duplicate keys. It can miss renamed or removed/reintroduced features, control-flow changes around an unchanged assertion, and algebraically equivalent settings; the certificate case demonstrates why code review is necessary. It is a conservative screen, not a census of every behavioral reversal. Changes within a worker branch are invisible to first-parent line lifetimes and are separately evidenced through reachable revert history. Lane attribution is provenance, not causation. The last week's incomplete follow-up prevents a mature full-week comparison, and a test or benchmark run is still required before any readiness conclusion.
