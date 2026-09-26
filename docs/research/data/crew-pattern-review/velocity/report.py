"""Render a compact findings record from the reproducible measurement."""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def number(value):
    return "unmeasured" if value is None else f"{value:,.2f}"


def main():
    data = json.loads((HERE / "summary.json").read_text())
    total = data["total"]
    product = sum(total["lines"][category]["added"] for category in ("source", "tests"))
    deleted = total["product_deleted_within_seven_days"]
    record = total["record_to_product_added_line_ratio"]
    timing = total["dispatch_to_completion_seconds"]
    promotion = total["dispatch_to_promotion_seconds"]
    attempts = total["attempts_per_landed_node"]
    durable = total["durable_crew_product_lines_per_worker_hour"]
    lines = [
        "# Crew velocity census",
        "",
        (
            f"The five primary branches received **{product:,} product additions** (source plus tests) in "
            f"{data['total']['primary_commits']['denominator']:,} first-parent commits during "
            "2026-09-12 00:00 UTC through 2026-09-26 10:00 UTC. "
            f"Of the {int(deleted['denominator']):,} additions with a full seven-day follow-up, "
            f"{int(deleted['numerator']):,} were deleted within seven days "
            f"({number(100 * deleted['value'])}%), leaving "
            f"**{total['durable_product_lines_seven_days']:,} seven-day durable additions**. "
            f"Another {total['right_censored_product_additions']:,} additions lack full follow-up. "
            "These are line identity counts, not a judgment about usefulness or correctness."
        ),
        "",
        (
            f"There are {total['promoted_nodes']['denominator']:,} promotion records: "
            f"{total['promoted_nodes']['implement_class']:,} implement-class and "
            f"{total['promoted_nodes']['review_investigate']:,} review/investigate. "
            f"Linking retry/repair families yields {total['logical_promoted_nodes']['denominator']:,} logical nodes "
            f"({total['logical_promoted_nodes']['implement_class']:,} implement-class; "
            f"{total['logical_promoted_nodes']['review_investigate']:,} review/investigate). "
            f"Across {int(attempts['denominator']):,} commit-bearing landed logical nodes, "
            f"{int(attempts['numerator']):,} recorded attempts imply **{number(attempts['value'])} attempts per landed node**. "
            "This is a lower bound: discarded attempts absent from committed ledgers and repairs without a declared or literal name link are not invented."
        ),
        "",
        (
            f"Record additions are {int(record['numerator']):,} lines versus {int(record['denominator']):,} product additions: "
            f"**{number(record['value'])} record lines per product line**. The record bucket is plan/evidence/research HTML, "
            "the figures directory, and docs/state. Binary figures are counted as file changes; their line counts stay unavailable. "
            f"The figures directory alone includes {total['figure_directory_support_added_lines']:,} support-data lines. "
            f"Excluding those gives {number(total['record_to_product_excluding_figure_support']['value'])}; "
            f"excluding the entire figure directory gives {number(total['record_to_product_excluding_figure_directory']['value'])} "
            f"({int(total['record_to_product_excluding_figure_directory']['numerator']):,}/{product:,}). "
            "The inclusive ratio measures stored output volume, not writing effort. Other documentation, configuration, lockfiles and data outside figures are a separate bucket."
        ),
        "",
        (
            f"Dispatch-to-completion median/p75 are **{number(timing['median'] / 60)} / {number(timing['p75'] / 60)} minutes** "
            f"(n={timing['denominator']:,}/{timing['population']:,}). Dispatch-to-promotion median/p75 are "
            f"**{number(promotion['median'] / 60)} / {number(promotion['p75'] / 60)} minutes** "
            f"(n={promotion['denominator']:,}/{promotion['population']:,}). Completion uses the ledger clock; promotion uses "
            "the earliest reachable promote(run-id) commit's committer clock where present, otherwise the first primary commit adding the run record, "
            "which is an upper bound on the promotion event. Promotion is not evidence that product code survived a merge. "
            f"Clock sources: {json.dumps(total['promotion_clock_sources'], sort_keys=True)}."
        ),
        "",
        (
            f"The timed, mature, attributed product cohort delivered **{number(durable['value'])} durable lines per worker-hour**: "
            f"{int(durable['numerator']):,} durable lines / {number(durable['denominator'])} recorded worker-hours "
            f"from {durable['denominator_runs']:,} runs. "
            f"{durable['unmatched_eligible_crew_product_additions']:,} eligible attributed additions lack a fully matched timed promotion cohort. "
            "This matched rate uses producing runs, not all review/failure overhead. The JSON also records the gross rate using all promoted worker-hours."
        ),
        "",
        "## Comparison with the published August baseline",
        "",
        (
            "The [August review](/reckon/research/crew-fleet-rox-review#s2) covers August 12-16, four projects, "
            "and 259 completed runs. Its figures are quoted, not recomputed. September adds imas-efit and measures "
            "primary-branch output, so the table does not imply an efficiency gain from larger totals."
        ),
        "",
        "| Measure | Published August | September census | Comparison limit |",
        "|---|---:|---:|---|",
        f"| Completed/promoted records | 259 completed | {total['promoted_nodes']['denominator']:,} promoted | Five calendar days/four projects versus 14 days 10 hours/five projects |",
        f"| Lines added | 108,587; three projects reporting | {product:,} source + test additions | August authored lines were not classified or restricted to primary landings |",
        f"| Commits | 228 | {total['primary_commits']['denominator']:,} first-parent commits | September includes record and merge commits |",
        f"| Median minutes per run | 9.8 | {number(timing['median'] / 60)} dispatch to completion | Clock sources and role mix differ; n={timing['denominator']:,} |",
        f"| Worker-hours | 66.2, stall corrected | {number(total['worker_hours']['value'])}, recorded | September is not stall corrected; n={total['worker_hours']['denominator_runs']:,} |",
        "| Median authored changed lines per worker-minute | 16-24 across three codebases | Not a comparable metric | Durable landed additions/hour is a different numerator, statistic and cohort |",
        f"| Durable lines per worker-hour | Not reported | {number(durable['value'])} | No August durability denominator |",
        f"| Seven-day deletion share | Not reported | {number(100 * deleted['value'])}% | Only mature September additions enter denominator |",
        f"| Record/product line ratio | Not reported | {number(record['value'])} | No August class split |",
        f"| Promotion latency; attempts/landed node | Not reported | {number(promotion['median'] / 60)} min median; {number(attempts['value'])} attempts | No comparable August figures |",
        "",
        "## Project comparison",
        "",
        "| Project | September promoted records | August completed records | September product adds | August reported adds | Mature adds deleted within 7d | Durable mature adds |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for cell in data["by_project"]:
        m = cell["metrics"]
        baseline = data["august_baseline"]["per_project"].get(cell["project"], {})
        loss = m["product_deleted_within_seven_days"]
        lines.append(
            f"| {cell['project']} | {m['promoted_nodes']['denominator']:,} | {baseline.get('completed_runs', 'not included')} | "
            f"{sum(m['lines'][k]['added'] for k in ('source', 'tests')):,} | {baseline.get('lines_added') if baseline.get('lines_added') is not None else 'not reported'} | "
            f"{int(loss['numerator']):,}/{int(loss['denominator']):,} | {m['durable_product_lines_seven_days']:,} |"
        )
    lines += [
        "",
        "## Lane comparison",
        "",
        "| Lane | Promoted implement records | Promoted review/investigate records | Attributed product adds | Mature durable adds | Matched durable lines / worker-hours |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for cell in data["by_lane"]:
        m = cell["metrics"]
        rate = m["durable_crew_product_lines_per_worker_hour"]
        lines.append(
            f"| {cell['lane']} | {m['promoted_nodes']['implement_class']:,} | {m['promoted_nodes']['review_investigate']:,} | "
            f"{sum(m['lines'][k]['added'] for k in ('source', 'tests')):,} | {m['durable_product_lines_seven_days']:,} | "
            f"{int(rate['numerator']):,} / {number(rate['denominator'])} = {number(rate['value'])} |"
        )
    lines += [
        "",
        (
            "The [durable-output figure](/reckon/figures/crew-pattern-review-velocity/durable-product-lines.png) "
            "shows the mature daily series and gross additions beneath it. September 19 has only a partial eligible cohort; "
            "later days are censored, not zero durable output."
        ),
        "",
        "## Denominators and limits",
        "",
        "- Each project, UTC day and lane has a cell, including explicit zero-population cells. Run days are promotion days; line days are primary landing days. Timing cells are cohorts, not calendar-time exposure.",
        "- Primary first-parent diffs count a merge once. Worker commit citations assign the entire primary landing diff to one lane, mixed lanes or unattributed. This measures an integration footprint, including merge resolution; it does not prove authorship of each line. Promotion/release/record commits with an explicit run id inherit that run's lane.",
        "- Lane changes use the final recorded backend. The JSON retains lineage so cross-lane work can be inspected rather than mistaken for a controlled comparison. Differences between lanes also reflect project, role and workload selection.",
        "- Line identity is tracked through insertions, edits and renames. Re-added text is a new line identity. Deletion is a churn measure, not a defect verdict; intentional simplification also deletes lines.",
        f"- The ledger supplies {total['worker_hours']['denominator_runs']:,}/{total['worker_hours']['population_runs']:,} worker-time observations. Sources: {json.dumps(total['worker_hours']['by_source'], sort_keys=True)}. Nonpositive durations are excluded; no missing duration is filled with zero.",
        f"- {len(total['worker_hours']['zero_duration_runs'])} rows report zero worker seconds, including {len(total['worker_hours']['zero_duration_with_commits'])} with commit citations. Their ids remain in the JSON; a zero counter is not credited as free product work.",
        f"- Coordinator active dispatch is {number(total['coordinator_active_dispatch_hours']['value'])} hours over {total['coordinator_active_dispatch_hours']['denominator_sessions']:,} recorded project/session pairs and {total['coordinator_active_dispatch_hours']['denominator_runs']:,}/{total['coordinator_active_dispatch_hours']['population_runs']:,} promotion records. It unions worker intervals per session and sums sessions; it is not measured human or coordinator interaction time. Gross attributed product/active-dispatch-hour is {number(total['gross_crew_product_lines_per_coordinator_active_dispatch_hour']['value'])}; the August report did not report this denominator.",
        "- The predecessor_run field is not used as an attempt link: the application can infer it from a matching base commit, which is integration ancestry rather than retry evidence.",
        "- Missing promotion clocks and unresolved/unreachable citations are listed by run in summary.json.coverage; they are not guessed from timestamps, author names or prose.",
        "",
    ]
    for item in data["coverage"]:
        lines.append(
            f"  - {item['project']}: {item['completed_in_window']:,} available records completed in the window; "
            f"{len(item['completed_in_window_without_promotion_clock']):,} lack a recoverable promotion clock; "
            f"{len(item['unresolved_or_unreachable_commit_citations']):,} contain unresolved or unreachable commit citations; "
            f"{len(item.get('recovered_from_sqlite', [])):,} rows were recovered from SQLite for independently reachable promotion commits; "
            f"{len(item.get('promotion_ids_without_record', [])):,} promotion ids still lack any record; "
            f"{len(item.get('ledger_clock_recoveries', [])):,} clocks come from first committed ledger appearance rather than an explicit promote commit."
        )
    lines += [
        "",
        "## Reproduction and checks",
        "",
        (
            "The full `velocity.json` and `inputs.json.gz` live in `/home/ITER/mcintos/.config/reckon/crew/runs/r-20260926T104517371666-velocity-census/`. "
            "The repository keeps `summary.json` under 300 KB with all cited figures, project/lane/week aggregates and named coverage rows. "
            "`inputs.json.gz` freezes reduced ledger records, primary heads, commit metadata, numeric file deltas and edit coordinates. "
            "It contains no transcript bodies or source text. A normal census run reads only this snapshot; `--capture` refuses to overwrite it."
        ),
        "",
        "```sh",
        "/home/ITER/mcintos/Code/reckon/.venv/bin/python docs/research/data/crew-pattern-review/velocity/census.py",
        "/home/ITER/mcintos/Code/reckon/.venv/bin/python docs/research/data/crew-pattern-review/velocity/verify.py",
        "/home/ITER/mcintos/Code/reckon/.venv/bin/python docs/research/data/crew-pattern-review/velocity/plot.py",
        "```",
        "",
        (
            "The verifier checks byte equality of both the full file in the run directory and compact file in the repository, the 300 KB compact bound, conservation across project/day/lane/week partitions, all five projects present, nonzero observed churn and censoring, "
            "and known line-identity controls. The full capture checks every primary product commit against its numstat addition count. "
            "The initial capture was rejected when zero-context diff matching disagreed with normal numstat (30/23 versus 28/21 on a real file). "
            "The corrected instrument parses normal-context diffs; both the failed capture and its replacement logs are retained with the run."
        ),
        "",
        "Pinned primary heads:",
        "",
    ]
    for branch in data["provenance"]["branches"]:
        lines.append(
            f"- {branch['project']} `{branch['primary_branch']}`: `{branch['head']}`"
        )
    lines += [
        "",
        f"Canonical input SHA-256: `{data['provenance']['input_sha256']}`.",
        "",
    ]
    (HERE / "findings.md").write_text("\n".join(lines))
    print(HERE / "findings.md")


if __name__ == "__main__":
    main()
