"""A context refusal names how its dominant term arrived.

The context-fit refusal reports the total estimate, the window it exceeded and
the largest contributing files with their token figures. It does not say how the
largest of those inputs arrived, and the two shapes have different remedies: a
declared write path cannot be removed from the brief, because the node must edit
it, while a path the brief only names in prose can be cut or replaced by a
reference. Without that fact a coordinator under budget pressure rationally cuts
the only field they control -- the prose -- and the refusal reads as an ordinary
success.

These cases drive routing's own verdict function, so the refusal is the payload
a dispatch emits, and the sentence is read back from the verdict rather than
hand-built.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reckon import capability, crew
from reckon.crew import routing

BACKEND = "clive"
DECLARED_WINDOW_TOKENS = 230_144
STANDING_INSTRUCTION_TOKENS = 20_000
LARGE_FILE_BYTES = 1_000_000
SMALL_FILE_BYTES = 3_500
TINY_FILE_BYTES = 350


def _backend_settings(window_tokens: int) -> dict[str, Any]:
    """The recorded lane settings copy a rebuilt dispatch reads the window from."""
    return {
        "backend": BACKEND,
        "launch": "cli",
        "model": "deepseek-v4-flash",
        "effort": "high",
        "sandbox": "worktree-full",
        "alias": "flash4",
        "usable_input_window": window_tokens,
        "local": True,
    }


def _refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    goal: str,
    write_paths: list[str],
    files: dict[str, int],
    window_tokens: int = DECLARED_WINDOW_TOKENS,
) -> dict[str, Any]:
    """Build a ``context-window-exceeded`` verdict through routing's verdict function."""
    monkeypatch.setattr(
        routing,
        "_standing_context_input",
        lambda *_args, **_kwargs: (
            STANDING_INSTRUCTION_TOKENS,
            {"effective_tokens": STANDING_INSTRUCTION_TOKENS},
        ),
    )
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    for relative, size in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x" * size, encoding="utf-8")
    resolution = crew.DispatchPlan(
        run_id="run",
        backend=BACKEND,
        launch="cli",
        backend_settings=_backend_settings(window_tokens),
        node=crew.TaskNode(
            id="node",
            goal=goal,
            plan="plan-a",
            estimated_hours=2.0,
            write_paths=write_paths,
        ),
        budget_ceiling="1h",
        validation=crew.NodeValidation(ok=True),
        execution_fit=capability.ExecutionFit(
            role="implement",
            execution_capable=True,
            matched_measure=None,
            override=False,
        ),
    )
    return routing._competence_verdict(
        resolution=resolution,
        project="alpha",
        repo=repo,
        verdict_inputs={
            "node_estimate": (2.0, "plan-fallback"),
            "capability_cache": {"configurations": []},
            "cache_status": "untracked",
        },
    )


def _dominant_clause(detail: str) -> str:
    """Return the dominant-term clause, failing loudly when the refusal omits it."""
    assert "dominant term:" in detail, detail
    return detail.split("dominant term:", 1)[1].strip()


def _repository_tokens(verdict: dict[str, Any], name: str) -> int:
    """Read one file's charged tokens from the payload's own per-file census."""
    groups = verdict["context"]["inputs"]["repository_files"]
    for group in ("write_paths", "named_files"):
        for item in groups[group]:
            if item["path"].endswith(name) and item["counted"]:
                return int(item["estimated_tokens"])
    raise AssertionError(f"{name} is not a charged repository input")


def test_a_refusal_names_its_dominant_term_as_a_declared_write_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The largest input is reported as a declared write path, with its tokens."""
    verdict = _refusal(
        tmp_path,
        monkeypatch,
        goal="exercise the dominant term; the brief declares small_module.py as an input.",
        write_paths=["large_module.py"],
        files={
            "large_module.py": LARGE_FILE_BYTES,
            "small_module.py": SMALL_FILE_BYTES,
        },
    )
    detail = verdict["detail"]

    clause = _dominant_clause(detail)
    assert "large_module.py" in clause
    assert "declared write path" in clause
    assert str(_repository_tokens(verdict, "large_module.py")) in clause

    # The total, the window and the largest-contributor list a reader already
    # relies on are unchanged: this adds a line rather than replacing evidence.
    assert detail.startswith(
        f"context-window-exceeded: estimated {verdict['estimated_tokens']} "
        f"usable input tokens against a {verdict['window_tokens']} token window "
        f"(shortfall {verdict['shortfall_tokens']} tokens); "
    )
    assert "largest contributing files: " in detail
    assert "large_module.py" in detail.split("largest contributing files: ", 1)[1]
    assert "small_module.py" in detail.split("largest contributing files: ", 1)[1]


def test_a_refusal_names_its_dominant_term_as_a_mention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A named file dominates its own write path, and is reported as a mention."""
    verdict = _refusal(
        tmp_path,
        monkeypatch,
        goal="exercise the dominant term; the brief declares large_module.py as an input.",
        write_paths=["small_module.py"],
        files={
            "large_module.py": LARGE_FILE_BYTES,
            "small_module.py": SMALL_FILE_BYTES,
        },
    )
    detail = verdict["detail"]

    clause = _dominant_clause(detail)
    assert "large_module.py" in clause
    assert "mention" in clause
    assert "declared write path" not in clause
    assert str(_repository_tokens(verdict, "large_module.py")) in clause

    # The small write path is still reported, and still charged at full load:
    # the origin names which input dominates, it does not reclassify the others.
    assert "small_module.py" in detail.split("largest contributing files: ", 1)[1]


def test_a_refusal_names_a_term_when_every_contribution_is_small(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A brief whose files are all small still gets an answer, not a blank."""
    verdict = _refusal(
        tmp_path,
        monkeypatch,
        goal="exercise the dominant term; the brief declares tiny_note.py as an input.",
        write_paths=["tiny_module.py"],
        files={"tiny_module.py": TINY_FILE_BYTES, "tiny_note.py": TINY_FILE_BYTES},
        window_tokens=20_150,
    )
    detail = verdict["detail"]

    clause = _dominant_clause(detail)
    assert "tokens)" in clause
    assert "declared write path" in clause or "mention" in clause


def test_a_refusal_says_none_dominates_when_no_file_contributes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no repository input charged, the refusal claims no dominant file."""
    verdict = _refusal(
        tmp_path,
        monkeypatch,
        goal="exercise the dominant term with no repository input at all",
        write_paths=[],
        files={},
        window_tokens=19_000,
    )
    detail = verdict["detail"]

    assert "no repository file dominates the estimate" in detail
    assert "dominant term:" not in detail
    assert verdict["estimated_tokens"] == STANDING_INSTRUCTION_TOKENS
