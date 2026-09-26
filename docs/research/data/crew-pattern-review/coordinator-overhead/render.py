"""Render the measured coordinator census as a compact report and scatter plot."""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
LABELS = {
    "other": "Other tools and uncategorised shell work",
    "git_and_merges": "Git and merges",
    "crew_reads_and_mcp_views": "Crew reads and MCP views",
    "reading_worker_diffs_and_gate_logs": "Reading diffs, manifests and gate logs",
    "dispatch": "Dispatch, resume and redispatch",
    "promotion": "Promotion",
    "plan_edits": "Plan, evidence and research edits",
    "follower_notifications": "Follower and monitor tool calls",
}


def main():
    data = json.loads((HERE / "overhead.json").read_text())
    totals = data["totals"]
    covered = [s for s in data["sessions"] if s["transcript_status"] == "captured"]
    denominator = totals["landed_nodes_with_transcript"]
    categories = sorted(
        totals["tool_calls"], key=lambda k: (-totals["tool_calls"][k], k)
    )
    lines = [
        "# Coordinator overhead census",
        "",
        (
            f"The fixed window is {data['window'][0]} through {data['window'][1]}, inclusive. "
            f"The census identifies **{totals['landed_nodes']:,} promoted runs** among "
            f"{totals['runs_in_cohort']:,} run records across five primary branches. "
            f"The {totals['sessions_with_transcript']} available coordinator transcripts account for "
            f"{denominator:,} promotions and {totals['assistant_turns']:,} distinct assistant API responses: "
            f"**{totals['assistant_turns_per_landed_node']:.2f} responses per promoted run**."
        ),
        "",
        "A promoted run is an administrative landing, not necessarily useful product work. "
        + "; ".join(
            f"{v:,} have gate `{k}`" for k, v in totals["landed_by_gate"].items()
        )
        + ". "
        "Role breakdown: "
        + ", ".join(f"{k} {v:,}" for k, v in totals["landed_by_role"].items())
        + ". "
        "Distinct run IDs are the node denominator; repairs and retries with different IDs remain separate.",
        "",
        "## The five largest activity costs per promoted run",
        "",
        (
            "The common unit here is observed tool calls per promoted run, not elapsed time or money. "
            "One shell call can contain several commands and one response can emit several calls. "
            "The classifier assigns each call once; the unclassified remainder is visible rather than distributed by guesswork."
        ),
        "",
        "| Rank | Activity | Calls | Calls / promoted run | Denominator |",
        "| ---: | --- | ---: | ---: | ---: |",
    ]
    for rank, key in enumerate(categories[:5], 1):
        lines.append(
            f"| {rank} | {LABELS[key]} | {totals['tool_calls'][key]:,} | "
            f"{totals['tool_calls_per_landed_node'][key]:.3f} | {denominator:,} |"
        )
    lines.extend(
        [
            "",
            "Remaining categories: "
            + "; ".join(
                f"{LABELS[k]} {totals['tool_calls'][k]:,} calls / {denominator:,} promotions "
                f"= {totals['tool_calls_per_landed_node'][k]:.3f}"
                for k in categories[5:]
            )
            + ".",
            "",
            (
                f"Separately, {totals['follower_notifications']:,} incoming obligation/fleet notification records "
                f"were observed ({totals['follower_notifications'] / denominator:.3f} per covered promotion). "
                "These are incoming records, not added to the exclusive tool-call total. They may repeat the same obligation."
            ),
            "",
            "## Context volume and the five most expensive sessions",
            "",
            (
                f"Total logical input is {totals['tokens']['input_tokens']:,} tokens "
                f"({totals['tokens_per_landed_node']['input_tokens']:,.0f} per covered promotion), of which "
                f"{totals['tokens']['cache_read_input_tokens']:,} are cache reads "
                f"({100 * totals['tokens']['cache_read_input_tokens'] / totals['tokens']['input_tokens']:.2f}%). "
                f"Output is {totals['tokens']['output_tokens']:,} tokens "
                f"({totals['tokens_per_landed_node']['output_tokens']:,.0f} per promotion). "
                "Logical input includes uncached input, cache creation and cache reads. These figures are not billable tokens, "
                "dollars, active work time, or unique text. No monetary spend is derived without a price/usage receipt."
            ),
            "",
            (
                "The session ranking uses logical input tokens per promotion. Whole-session work inside the window "
                "is counted, including non-crew work; small denominators and mixed responsibilities can dominate."
            ),
            "",
            "| Session / project | Promotions | API responses / promotion | Input tokens / promotion | Output tokens / promotion |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    top = sorted(
        (s for s in covered if s["landed_nodes"]),
        key=lambda s: -s["tokens_per_landed_node"]["input_tokens"],
    )[:5]
    lines.extend(
        f"| `{session['session_id']}` / {', '.join(session['projects'])} | "
        f"{session['landed_nodes']:,} | {session['assistant_turns_per_landed_node']:.2f} | "
        f"{session['tokens_per_landed_node']['input_tokens']:,.0f} | "
        f"{session['tokens_per_landed_node']['output_tokens']:,.0f} |"
        for session in top
    )
    lines.extend(
        [
            "",
            (
                "The [session scatter plot](/reckon/figures/orchestrator-crew-pattern-studies/coordinator-effort.svg) "
                "shows the relationship between promotion count and logical input per promotion. Its horizontal line is the "
                "pooled observed rate, not a fitted causal model."
            ),
            "",
            "## Refusals and recovery",
            "",
            "| Operation | Observed refusals | Positive success receipts | Recovered before window end | Mean responses to next family success | Same-target recoveries | Mean responses to same-target success |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for name in ("dispatch", "promotion"):
        row = totals[name + "_refusals"]
        same = row["turns_to_next_success_same_target_mean"]
        lines.append(
            f"| {name} | {row['refusals']:,} | {row['success_receipts']:,} | "
            f"{row['recovered_refusals']:,} / {row['refusals']:,} | "
            f"{row['turns_to_next_success_mean']:.2f} | {row['same_target_recovered_refusals']:,} | {same:.2f} |"
        )
    lines.extend(
        [
            "",
            "Dispatch error codes: "
            + "; ".join(
                f"`{k}` {v}"
                for k, v in sorted(
                    totals["dispatch_refusals"]["by_error_code"].items(),
                    key=lambda kv: -kv[1],
                )
            )
            + ".",
            "",
            "Promotion reason groups: "
            + "; ".join(
                f"`{k}` {v}"
                for k, v in sorted(
                    totals["promotion_refusals"]["by_reason"].items(),
                    key=lambda kv: -kv[1],
                )
            )
            + ".",
            "",
            (
                "Exact refusal text, source file/line, timestamp and recovery interval are retained in the hashed attempts artifact. "
                "Reason groups are text classifications; `cli-error` means the CLI emitted an Error line without a structured code. "
                "A positive receipt may be full JSON, a printed Python dictionary, or a clipped success marker; receipt shapes are counted "
                "in the JSON. A quiet shell is never counted as success. The next success in an operation family need not retry the "
                "same node; same-target columns require a literal target join. Missing successes and unidentified targets stay null. "
                "Intervals overlap and contain other work, so their sums cannot be interpreted as wasted labor."
            ),
            "",
            (
                f"**Coverage limit:** {totals['attempt_calls_without_classifiable_receipt']:,} candidate attempt calls have no "
                "classifiable receipt or have ambiguous mixed command families. Shell clipping, redirection, helper scripts and "
                "unavailable output prevent an exhaustive refusal count; these figures are observed lower bounds. "
                "Calls to help and dry runs are excluded from attempt accounting."
            ),
            "",
            "## Documentation merge conflicts",
            "",
            "| Primary branch | Merges touching plans/evidence | Replay conflicts anywhere | Replay conflicts in plans/evidence | Unmeasured |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for project, row in sorted(data["primary_branches"].items()):
        lines.append(
            f"| {project}/{row['primary_branch']} | {row['documentation_merges']:,} | "
            f"{row['conflicting_any_path']:,} | {row['conflicting_docs_path']:,} | {row['unmeasured']} |"
        )
    merge = totals["merges"]
    lines.extend(
        [
            "",
            (
                f"Across {merge['documentation_merges']:,} qualifying merges, "
                f"{merge['conflicting_any_path']:,} ({100 * merge['conflicting_any_path'] / merge['documentation_merges']:.2f}%) "
                f"conflict somewhere when their recorded parents are replayed; {merge['conflicting_docs_path']:,} "
                f"({100 * merge['conflicting_docs_path'] / merge['documentation_merges']:.2f}%) conflict in the named documentation paths. "
                "These are current `git merge-tree` replays, not a count of historical manual resolutions. The sample is first-parent "
                "primary-branch merges whose resulting tree differs from the first parent under docs/plans or docs/evidence; "
                "merges with no net documentation change are excluded. Conflict exits and operational errors are distinguished. "
                "Object writes are redirected to isolated temporary directories."
            ),
            "",
            "## Missing coverage and unmeasured work",
            "",
            (
                f"- {totals['sessions_without_transcript']} of {totals['sessions']} recorded coordinator sessions lack usable transcript evidence: "
                f"{totals['sessions_missing_transcript_file']} files are missing and {totals['sessions_with_empty_window_snapshot']} snapshot has no in-window records. They account for {totals['landed_nodes'] - denominator:,} of "
                f"{totals['landed_nodes']:,} promotions. Their turns/tokens are null and excluded from the cost denominator."
            ),
            (
                f"- {totals['unattributed_runs']} run records lack a runtime session ID, in "
                f"{totals['unattributed_session_groups']} unattributed groups. Every observed runtime identity came from "
                "node_definition.coordinator.runtime_session_id. Top-level worker session IDs were not substituted."
            ),
            (
                "- All selected coordinator harness records identify the message-style session format. Worker `thread.started` "
                "streams are not inputs to this study, so no claim of their emptiness or activity is made."
            ),
            (
                "- **Hand-committed worker diffs: not measured.** Successful coordinator commit calls are retained as candidates, "
                "but a commit receipt alone does not establish worker authorship. Quiet commits, shell variables, copied patches "
                "and edits made on the primary checkout require an additional provenance join. The reported count is null; "
                "zero matches to the narrow explicit-worktree command probe is not evidence that hand integrations were absent."
            ),
            (
                "- Tool classification is deterministic and exclusive but heuristic. Shell commands are tokenized so quoted "
                "goals/advice do not masquerade as executed commands. Compound calls get one category, not one count per subprocess. "
                "No duration or token cost is assigned to individual tool categories."
            ),
            "",
            "## Reproduction and evidence",
            "",
            "Run from this directory with the project's root virtual environment:",
            "",
            "```bash",
            "/home/ITER/mcintos/Code/reckon/.venv/bin/python census.py",
            "/home/ITER/mcintos/Code/reckon/.venv/bin/python verify.py --rerun",
            "```",
            "",
            (
                "The first command reparses fixed transcript snapshots and reruns merge-tree against pinned parent objects. "
                "The second checks recorded positive controls, coverage invariants and SHA-256 identities, then compares a fresh "
                "census byte for byte. `inputs.json` pins transcript byte extents, source hashes, branch revisions and frozen "
                "inputs under the durable run directory. `overhead.json` holds compact session/totals data and absolute path/hash "
                "references to larger artifacts. These external files must be retained; this is not a standalone corpus archive "
                "inside Git. `census.log` and `verification.log` name the tested revision and command. All committed files remain "
                "below 300 KB. `render.py` regenerates this report and the plot from the measured JSON."
            ),
            "",
            (
                "The incomplete inherited draft was preserved before repair. A known duplicate transcript message is asserted as "
                "111,913 input and 197 output tokens once, despite repeated content records. A real missing-gate refusal exercises "
                "the Error-line parser; both clean and conflicting merge replays are retained as positive controls. "
                "No mutation negative control applies to this measurement node; byte-identical re-execution is its declared gate."
            ),
            "",
            (
                "The next concrete action is coordinator integration and independent verification at the merged head. "
                "For a complete hand-integration census, commission the worker-diff/primary-commit provenance join using the retained "
                "candidate artifact. Recovery of missing or empty transcript evidence would extend coverage; it must not silently change this frozen sample."
            ),
        ]
    )
    (HERE / "findings.md").write_text("\n".join(lines) + "\n")
    figure_root = HERE.parents[3] / "figures/orchestrator-crew-pattern-studies"
    figure_root.mkdir(parents=True, exist_ok=True)
    plotted = [
        s for s in covered if s["landed_nodes"] and s["tokens"]["input_tokens"] > 0
    ]
    plt.rcParams.update(
        {"font.size": 13, "svg.fonttype": "none", "svg.hashsalt": "coordinator-census"}
    )
    fig, ax = plt.subplots(figsize=(11, 6))
    ax.scatter(
        [s["landed_nodes"] for s in plotted],
        [s["tokens_per_landed_node"]["input_tokens"] / 1e6 for s in plotted],
        color="#176b87",
        s=32,
        alpha=0.8,
    )
    mean = totals["tokens_per_landed_node"]["input_tokens"] / 1e6
    ax.axhline(mean, color="#555555", linewidth=0.8, linestyle="--")
    ax.text(
        0.98,
        mean * 1.07,
        f"Pooled rate: {mean:.2f} million",
        transform=ax.get_yaxis_transform(),
        ha="right",
        va="bottom",
        color="#333333",
    )
    ax.set_yscale("log")
    ax.set_xlabel("Promoted runs in the session")
    ax.set_ylabel("Input tokens / promotion (millions)")
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", which="major", linewidth=0.4, alpha=0.3)
    ax.set_xlim(left=0)
    fig.text(
        0.12,
        0.02,
        f"{len(plotted)} sessions with promotions and usable input; log y-axis. Cached tokens included.",
        fontsize=12,
    )
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    target = figure_root / "coordinator-effort.svg"
    fig.savefig(target, metadata={"Date": None})
    inputs = json.loads((HERE / "inputs.json").read_text())
    fig.savefig(Path(inputs["evidence_directory"]) / "coordinator-effort.png", dpi=140)
    plt.close(fig)
    print("rendered", HERE / "findings.md", target)


if __name__ == "__main__":
    main()
