"""Render the structural census findings and comparative trend figure."""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt


def main():
    here = Path(__file__).resolve().parent
    data = json.loads((here / "code-depth.json").read_text())
    series = {
        scope: [s["scopes"][scope] for s in data["snapshots"] if scope in s["scopes"]]
        for scope in ("reckon/", "reckon/crew/", "nova/")
    }
    latest = {
        repo: next(s for s in reversed(data["snapshots"]) if s["repository"] == repo)
        for repo in ("reckon", "nova")
    }
    candidates = data["ranked_candidates"]
    (here / "consolidation.json").write_text(
        json.dumps(candidates, indent=2, sort_keys=True) + "\n"
    )

    lines = [
        "# Code depth, interface width and reuse",
        "",
        "Reckon grew rapidly while its overall clone share fell; its crew subset grew faster and became more duplicated. Nova grew more slowly, but its clone share rose. Neither trend establishes causation by a worker lane or a change in scientific correctness. The useful result is a ranked set of shared implementation opportunities, not a claim that every small function is shallow.",
        "",
        "The census measures eight pinned repository snapshots and twelve scope rows. `reckon/crew/` is a subset of `reckon/`, not a third repository. All paths and line numbers below resolve at the named snapshot commit, not at a moving branch head.",
        "",
        "## Snapshot identities and time offsets",
        "",
        "Selection uses the nearest committer timestamp on pinned primary-branch first-parent history, limited to the capture cutoff of 2026-09-26 10:00Z. Weekly labels are targets, not claims that a commit exists exactly at noon. This explicitly bounds the September 26 noon target, which is later than capture. All source reads use git archive and temporary extracted Python files; neither target package is executed.",
        "",
        "| Repository | Noon target | Commit | Actual commit time | Offset, hours |",
        "|---|---|---|---|---:|",
    ]
    lines.extend(
        f"| {snapshot['repository']} | {snapshot['target'][:10]} | `{snapshot['commit']}` | {snapshot['committed_at']} | {snapshot['offset_seconds'] / 3600:+.2f} |"
        for snapshot in data["snapshots"]
    )
    lines += [
        "",
        "Reckon's September 12 target lands on September 14; nova's September 26 target lands on September 24. Growth rates are therefore comparisons between the reported snapshots, not uniform seven-day throughput measurements.",
        "",
        "## Size, surface and implementation lengths",
        "",
        "Source/test lines below are physical Python lines, including comments, blank lines and docstrings. The population includes package-contained scripts but excludes top-level scripts and other languages. Public functions include public methods under public classes; public classes include nested public classes. These are definitions, including separate property accessors, not measured exports or unique API attributes. Reexports, instance fields and dynamic registrations are outside this definition.",
        "",
        "| Scope | Target | Source lines | Test lines | Modules | Public functions / classes | Function median / p90 | Pure forwarders / all functions |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for snapshot in data["snapshots"]:
        for scope, m in snapshot["scopes"].items():
            lengths = m["function_length_distribution"]
            lines.append(
                f"| {scope} | {snapshot['target'][5:10]} | {m['source_lines']:,} | {m['test_lines']:,} | {m['module_count']} | {m['public_function_count']} / {m['public_class_count']} | {lengths['median']:g} / {lengths['p90']:g} | {m['pass_through_function_count']} / {lengths['count']} |"
            )
    lines += [
        "",
        "Crew test lines count whole files importing or naming `reckon.crew`; they overlap the total and can test additional concerns. Function length is the inclusive def-to-end physical span, including docstrings and excluding decorators. The compact JSON also carries min, p75, p95 and max; the full run-directory JSON retains the complete length histogram. A pure forwarder has one call/return after removing its docstring and passes each argument once without transformations; import-and-forward wrappers are deliberately outside that strict count.",
        "",
        "At the last snapshot, pure forwarders are 21/2,621 reckon functions (0.80%), 8/1,171 crew functions (0.68%), and 108/7,245 nova functions (1.49%). Those denominators include private and nested functions. This narrow instrument does not support a claim that these codebases consist mainly of trivial wrappers.",
        "",
        "## Clone share and interface trend",
        "",
        "The clone numerator is the union of token-bearing source lines participating in repeated six-line windows, counting both copies once. The denominator excludes blank lines, comments and docstrings. Whitespace and string/number values are normalized; identifiers and operators are preserved. Copies must be non-overlapping. Longer blocks appear as overlapping windows but their covered lines are unioned. This conservative detector misses renamed-variable and semantic clones, and literal normalization can group unrelated logic: matches require review before deletion.",
        "",
        "| Scope | Target | Cloned code lines / all code lines | Clone share | Weekly change, percentage points | Public surface | Weekly change |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for scope, rows in series.items():
        for index, m in enumerate(rows):
            clone = m["clones"]
            previous = rows[index - 1] if index else None
            delta = (
                f"{(clone['clone_share'] - previous['clones']['clone_share']) * 100:+.3f}"
                if previous
                else "baseline"
            )
            width_delta = (
                f"{m['public_surface'] - previous['public_surface']:+d}"
                if previous
                else "baseline"
            )
            lines.append(
                f"| {scope} | {('09-05', '09-12', '09-19', '09-26')[index]} | {clone['cloned_source_code_lines']:,} / {clone['source_code_lines']:,} | {clone['clone_share'] * 100:.3f}% | {delta} | {m['public_surface']:,} | {width_delta} |"
            )
    lines += [
        "",
        "Overall, reckon clone share falls 1.028 percentage points while public surface grows 82.9%; crew clone share rises 1.564 points while public surface grows 226.0%; nova clone share rises 0.844 points while public surface grows 5.7%. Reckon's cloned lines still grow from 4,246 to 6,843 despite its falling share. A falling percentage is not shrinking duplicate maintenance work.",
        "",
        "| Reckon target | CLI commands | CLI groups | CLI option occurrences | Distinct flags | MCP tool/view pairs | Distinct MCP names | Coded refusal families |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for snapshot in data["snapshots"]:
        if snapshot["repository"] == "reckon":
            i = snapshot["scopes"]["reckon/"]["interfaces"]
            lines.append(
                f"| {snapshot['target'][5:10]} | {i['cli_command_count']} | {i['cli_group_count']} | {i['cli_option_count']} | {i['cli_option_unique_flag_count']} | {i['mcp_view_count']} | {i['mcp_unique_view_name_count']} | {i['dispatch_refusal_code_count']} |"
            )
    lines += [
        "",
        "Reckon's CLI command changes are 0, +1, +5 week over week; option changes are +7, +7, +23; MCP tool/view changes are +1, 0, +3. The crew directory itself has no command decorators or MCP view declarations because those live in the parent package's CLI/MCP modules; that zero does not mean crew has no interface. The coded-refusal count measures `format_refusal` families, not all guard conditions. The first snapshot predates those codes but has dispatch raise sites, retained in JSON. The real PID-existence primitive is detected once at the final reckon snapshot, so it does not enter a three-copy reimplementation group.",
        "",
        "Nova's incidental Click surface is unchanged at three commands and fourteen option occurrences; its relevant width signal here is public definitions, growing +208, +87, +12. Its MCP and coded crew-refusal counts are zero and are not used as measures of its scientific API.",
        "",
        "## Modules with wide public surfaces relative to implementation",
        "",
        "Ranked below within each repository by public definitions per 100 token-bearing lines, requiring at least five public definitions. These are review leads: protocol declarations, schema models, property accessors and service facades can be appropriately small.",
        "",
        "| Repository | Module | Public definitions | Code lines | Public definitions per 100 code lines |",
        "|---|---|---:|---:|---:|",
    ]
    for repo, snapshot in latest.items():
        widths = snapshot["scopes"][repo + "/"]["module_surfaces"]
        widest = sorted(
            (m for m in widths if m["public_surface"] >= 5),
            key=lambda m: (-m["public_surface_per_100_code_lines"], m["path"]),
        )[:5]
        for m in widest:
            lines.append(
                f"| {repo} | `{m['path']}` | {m['public_surface']} | {m['code_lines']} | {m['public_surface_per_100_code_lines']:.2f} |"
            )
    lines += [
        "",
        "## Ten consolidation candidates, ranked by expected value",
        "",
        "Ranking prioritizes behavior that controls persistence, accounting or numerical contracts, then copy count and migration complexity. It is engineering judgment, not a measured savings estimate. The complete machine census retains every group found by its four disclosed detectors: direct primitive use, repeated module-level helper names, identical AST bodies and six-line normalized bodies. The detectors overlap; group counts cannot be summed as unique concepts. Primitive-use groups include clients, and same-name groups can be unrelated. Semantic equivalence outside those detectors is unmeasured.",
        "",
    ]
    for c in candidates:
        lines += [
            f"### {c['rank']}. {c['title']}",
            "",
            f"{c['reason']} Proposed single home: `{c['home']}`.",
            "",
            f"Constraint: {c['guard']}",
            "",
            f"Copies ({len(c['copies'])} definitions, commit `{c['commit']}`):",
            "",
        ]
        lines.extend(
            f"- `{copy['path']}:{copy['line']}` — `{copy['function']}`."
            for copy in c["copies"]
        )
        lines.append("")
    lines += [
        "Repeated `main`, `resolve`, or `fit` names are not a consolidation mandate. Likewise, five identical config-home bodies in standalone hooks are retained as candidates in the raw census but ranked below this list: removing their bootstrap independence needs its own design justification. Calling `np.diff`, `np.interp` or a digest primitive from multiple scientific operations is usually reuse of a library, not proof of reimplementing a helper.",
        "",
        "## Reproduction and evidence limits",
        "",
        "The compact repository JSON retains report figures and ranked copies. Histograms, all module and function locations, complete helper groups and raw clone windows live only in the full census at `"
        + data["full_output"]["path"]
        + "`. The compact file records that artifact's byte size and SHA-256.",
        "",
        "Run from this repository with its root environment: `UV_PROJECT_ENVIRONMENT=/home/ITER/mcintos/Code/reckon/.venv uv run --no-sync python docs/research/data/crew-pattern-review/code-depth/census.py --check`. The default sibling repository root is `/home/ITER/mcintos/Code`; override it with `--repo-root`. The checked-in pins are inputs, so a growing branch cannot change this result. Python must understand the snapshots' syntax; the recorded interpreter is Python 3.14.2. No environment synchronization, package import, test execution, external API or model request is needed.",
        "",
        "`census.log` records the complete measurement and the full-output path; `reproduction.log` records the second run and byte-identity receipt. Controls exercise a known twelve-line duplicate, a single-copy non-match, forwarding versus transformation, CLI/options/MCP/refusal extraction, and file replacement versus dataclass replacement. `render.py` reproduces this report, the ranked candidate JSON and the trend figure from `code-depth.json`.",
        "",
        "This is a static structural assessment. It does not measure call-site reuse, runtime exports, branch complexity, test quality, performance, scientific correctness or worker-lane causation. No production source changed. Each proposed consolidation needs a separately scoped implementation and behavior-preservation gate; the coordinator owns that sequencing and the combined review.",
        "",
    ]
    (here / "findings.md").write_text("\n".join(lines))

    plt.rcParams.update(
        {"font.size": 11, "svg.fonttype": "none", "svg.hashsalt": "source-structure"}
    )
    figure, axes = plt.subplots(1, 2, figsize=(11.8, 4.4), layout="constrained")
    styles = {
        "reckon/": ("#176b9a", "-", "o"),
        "reckon/crew/": ("#222222", "--", "s"),
        "nova/": ("#83542f", "-", "^"),
    }
    for scope, rows in series.items():
        color, linestyle, marker = styles[scope]
        axes[0].plot(
            range(4),
            [m["clones"]["clone_share"] * 100 for m in rows],
            color=color,
            linestyle=linestyle,
            marker=marker,
            label=scope,
            linewidth=1.8,
        )
        axes[1].plot(
            range(4),
            [m["public_surface"] / rows[0]["public_surface"] * 100 for m in rows],
            color=color,
            linestyle=linestyle,
            marker=marker,
            label=scope,
            linewidth=1.8,
        )
    axes[0].set_ylabel("Cloned token-bearing source lines (%)")
    axes[0].set_ylim(0, 14)
    axes[0].set_title("Duplicate share diverges within reckon", loc="left", fontsize=12)
    axes[1].set_ylabel("Public definition count (first snapshot = 100)")
    axes[1].set_ylim(0, 350)
    axes[1].set_title("Crew's public surface grows fastest", loc="left", fontsize=12)
    for ax in axes:
        ax.set_xticks(range(4), ["Sep 05", "Sep 12", "Sep 19", "Sep 26"])
        ax.set_xlabel("Weekly target, 2026; see commit offsets in evidence")
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", color="#dddddd", linewidth=0.5)
        ax.set_axisbelow(True)
    axes[0].legend(frameon=False, ncols=3, loc="lower left")
    out = here.parents[4] / "docs/figures/orchestrator-crew-pattern-studies"
    out.mkdir(parents=True, exist_ok=True)
    figure.savefig(
        out / "code-depth-trends.svg",
        metadata={"Date": "2026-09-26", "Creator": "Structural census"},
    )
    figure.savefig(out / "code-depth-trends.png", dpi=160)
    plt.close(figure)
    print("wrote findings.md, consolidation.json and code-depth-trends.svg/png")


if __name__ == "__main__":
    main()
