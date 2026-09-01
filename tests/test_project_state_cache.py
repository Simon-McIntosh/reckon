import json
import os
from pathlib import Path

from reckon import _plan_html
from reckon.project_state import compose_project_state


def _write_plan(path: Path, slug: str, status: str) -> None:
    path.write_text(
        f"""<!doctype html>
<html><head>
<meta name="docs-project" content="sample">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{slug.title()}">
<meta name="plan-status" content="{status}">
<meta name="plan-impl" content="0.0">
</head><body><main><h2>Work</h2></main></body></html>
""",
        encoding="utf-8",
    )


def _write_index(docs: Path) -> None:
    state_dir = docs / "state" / "sample"
    state_dir.mkdir(parents=True)
    state_dir.joinpath("index.json").write_text(
        json.dumps(
            {
                "project": "sample",
                "data": {
                    "sprints": [
                        {
                            "id": "current",
                            "status": "active",
                            "items": [{"slug": "alpha"}, {"slug": "beta"}],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )


def test_composition_reuses_each_parse_and_reparses_only_replaced_plan(
    tmp_path, monkeypatch
):
    docs = tmp_path / "docs"
    plans = docs / "plans"
    plans.mkdir(parents=True)
    _write_index(docs)
    alpha = plans / "alpha.html"
    beta = plans / "beta.html"
    _write_plan(alpha, "alpha", "active")
    _write_plan(beta, "beta", "pending")

    parse_count = 0
    original_read_state = _plan_html.read_state

    def counted_read_state(source):
        nonlocal parse_count
        parse_count += 1
        return original_read_state(source)

    monkeypatch.setattr(_plan_html, "read_state", counted_read_state)

    first = compose_project_state(docs, "sample")
    second = compose_project_state(docs, "sample")
    assert parse_count == 2
    assert first["sprints"] == second["sprints"]

    original_mtime_ns = alpha.stat().st_mtime_ns
    replacement = plans / "alpha.replacement"
    _write_plan(replacement, "alpha", "shipped")
    os.utime(replacement, ns=(original_mtime_ns, original_mtime_ns))
    os.replace(replacement, alpha)
    assert alpha.stat().st_mtime_ns == original_mtime_ns

    refreshed = compose_project_state(docs, "sample")
    assert parse_count == 3
    statuses = {
        item["slug"]: item["status"] for item in refreshed["sprints"][0]["items"]
    }
    assert statuses == {"alpha": "shipped", "beta": "pending"}
