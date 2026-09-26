"""Preserve selected code histories, marker observations and exact restoration checks."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from census import BASE, HEAD, RUN_DIRECTORY, Git, lane


def main():
    directory = Path(__file__).resolve().parent
    git = Git("/home/ITER/mcintos/Code/nova")
    data = json.loads((RUN_DIRECTORY / "flapping.json").read_text())
    primary = {item["sha"]: item for item in data["commits"]}
    runs = json.loads(git.read("show", HEAD + ":docs/state/nova/crew.json"))["data"][
        "runs"
    ]
    inputs = json.loads((directory / "episode-inputs.json").read_text())
    records = []
    patches = []
    for selection in inputs:
        path = selection["path"]
        snapshots = []
        contents = {}
        for sha in selection["commits"]:
            if sha in primary:
                landing = sha
            else:
                ancestry = git.read(
                    "log",
                    "--ancestry-path",
                    "--reverse",
                    "--topo-order",
                    "--format=%H",
                    sha + ".." + HEAD,
                ).splitlines()
                landing = next(item for item in ancestry if item in primary)
            matched = [
                {
                    "run_id": run["run_id"],
                    "lane": lane(run),
                    "backend": run.get("backend"),
                    "plan": run.get("plan"),
                    "join": "explicit_ledger_commit",
                }
                for run in runs
                if any(sha.startswith(citation) for citation in run.get("commits", []))
            ]
            parent = git.read("rev-parse", sha + "^1").strip()
            content = git.read("show", sha + ":" + path, binary=True)
            contents[sha] = content
            before = git.read("show", parent + ":" + path, binary=True)
            patch = git.read(
                "diff", "--no-renames", "--unified=4", parent, sha, "--", path
            )
            patches.append(f"COMMIT {sha}\n{patch}")
            marker_states = {}
            for marker in selection["markers"]:
                marker_states[marker] = {
                    "before": marker.encode() in before,
                    "after": marker.encode() in content,
                }
            snapshots.append(
                {
                    "sha": sha,
                    "parent": parent,
                    "committed_at": git.read("show", "-s", "--format=%cI", sha).strip(),
                    "subject": git.read("show", "-s", "--format=%s", sha).strip(),
                    "file_sha256": hashlib.sha256(content).hexdigest(),
                    "file_bytes": len(content),
                    "markers": marker_states,
                    "explicit_attribution": matched,
                    "primary_landing": landing,
                    "containing_landing_attribution": primary[landing]["attribution"],
                    "diff_numstat": git.read(
                        "diff", "--no-renames", "--numstat", parent, sha, "--", path
                    ).strip(),
                }
            )
        equalities = []
        for before_sha, after_sha in selection["equal_content_pairs"]:
            equal = contents[before_sha] == contents[after_sha]
            assert equal, (selection["topic"], before_sha, after_sha)
            equalities.append(
                {"before": before_sha, "after": after_sha, "byte_identical": equal}
            )
        records.append(
            {
                "topic": selection["topic"],
                "path": path,
                "snapshots": snapshots,
                "whole_file_restorations": equalities,
            }
        )
    result = {"head": HEAD, "base": BASE, "episodes": records}
    (RUN_DIRECTORY / "episode-evidence.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    (RUN_DIRECTORY / "episode-diffs.patch").write_text("\n".join(patches))
    print(
        f"Preserved {len(records)} episodes; all selected commits and whole-file restoration claims verified"
    )
    for episode in records:
        if episode["whole_file_restorations"]:
            print(json.dumps(episode["whole_file_restorations"]))
    print("review_module=" + str(Path(__file__).resolve()))
    print("review_cwd=" + str(Path.cwd().resolve()))
    print(
        "review_revision="
        + subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    )


if __name__ == "__main__":
    main()
