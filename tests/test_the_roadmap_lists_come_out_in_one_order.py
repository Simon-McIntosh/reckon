"""The roadmap's lists come out in one order.

The report visits each plan to append its decision-blocker and wiring rows. The
collection that ordered the visit was a set, so the order of ``decision_blockers``
and ``wiring_findings`` depended on the process hash seed: two reads of an
unchanged project returned the same rows in different orders, and any comparison
of roadmap output held only under a pinned ``PYTHONHASHSEED``.

These cases render one fixed docs tree in two subprocesses under different seeds
and require the JSON to be byte-identical. The rows themselves are unchanged by
the fix — only their order is derived from the plan slug.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

_SLUGS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]

# Renders the roadmap of the docs tree named by argv[1] and writes its JSON to
# stdout, so each subprocess's output can be compared byte for byte.
_RENDER = textwrap.dedent(
    """
    import json
    import sys
    from pathlib import Path

    from reckon.roadmap import build_roadmap
    from reckon.serve import discover_plans

    docs = Path(sys.argv[1])
    discovered = discover_plans(docs, "sample", docs.parent / "state")
    result = build_roadmap(
        "sample",
        discovered["inventory"],
        discovered["sprints"],
        active_sprint_id=discovered["active_sprint_id"],
        project_manifest=discovered,
        review={},
        docs_dir=docs,
    )
    sys.stdout.write(json.dumps(result, sort_keys=False))
    """
)


def _plan_document(slug: str) -> str:
    decisions = "".join(
        f'<div class="r-dec" data-key="d{n}" data-choice="" data-by="" '
        f'data-when=""><p class="r-dec-q">d{n}: which way?</p></div>'
        for n in (1, 2)
    )
    return (
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-effort" content="M">'
        f'<meta name="plan-depends-on" content="missing-{slug}">'
        f"<title>{slug}</title></head><body>"
        '<main class="plan-doc">'
        '<section data-reckon="gates">'
        '<div class="r-gate" data-id="evidence" data-status="closed" '
        'data-verdict="passed"></div></section>'
        '<section data-reckon="decisions" id="decisions" class="r-decisions">'
        f"{decisions}</section>"
        '<section data-reckon="followups">'
        '<article class="r-fu" data-id="next" data-status="open">'
        '<h4 class="r-fu-title">Continue</h4><div class="r-fu-body"></div>'
        '<pre class="r-fu-prompt">/reckon-build work</pre>'
        "</article></section></main></body></html>"
    )


def _docs_tree(tmp_path: Path) -> Path:
    docs = tmp_path / "docs"
    (docs / "plans").mkdir(parents=True, exist_ok=True)
    for slug in _SLUGS:
        (docs / "plans" / f"{slug}.html").write_text(
            _plan_document(slug), encoding="utf-8"
        )
    return docs


def _render(docs: Path, seed: str) -> bytes:
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = seed
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{_REPO_ROOT}{os.pathsep}{existing}" if existing else str(_REPO_ROOT)
    )
    completed = subprocess.run(
        [sys.executable, "-c", _RENDER, str(docs)],
        capture_output=True,
        env=env,
        check=True,
    )
    return completed.stdout


def test_roadmap_output_is_identical_under_two_hash_seeds(tmp_path: Path) -> None:
    docs = _docs_tree(tmp_path)

    first = _render(docs, "1")
    second = _render(docs, "104729")

    # The tree exercises both reported lists, so the comparison is not vacuous.
    report = json.loads(first)
    assert report["decision_blockers"], "tree produced no decision blockers"
    assert report["wiring_findings"], "tree produced no wiring findings"

    # The order is keyed by plan identity, so the rows come out by slug.
    plans = [row["plan"] for row in report["decision_blockers"]]
    assert plans == sorted(plans)

    assert first == second
