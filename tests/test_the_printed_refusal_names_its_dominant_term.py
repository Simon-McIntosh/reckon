"""The context refusal dispatch prints names its dominant term.

The refusal's structured payload already carries a per-file breakdown, but the
line the CLI prints states only the total, the window and the shortfall. A
coordinator reading that line is left guessing which input to cut and
rationally cuts the only one they control.

These cases build the verdict through routing's own verdict function, wrap it in
the refusal the dispatch CLI renders, and read the sentence back from that
refusal rather than hand-building it. The three shapes have different remedies:
a declared write path cannot leave the brief because the node edits it, a
mention can be cut or replaced by a reference, and a refusal where no repository
file dominates tells the coordinator the brief is not the problem at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reckon import capability, crew
from reckon.crew import routing
from reckon.crew.node import CompetenceLimit

BACKEND = "clive"
DECLARED_WINDOW_TOKENS = 230_144
STANDING_INSTRUCTION_TOKENS = 20_000
LARGE_FILE_BYTES = 1_000_000
SMALL_FILE_BYTES = 3_500


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
) -> CompetenceLimit:
    """Build the refusal a dispatch prints, from routing's own verdict."""
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
    verdict = routing._competence_verdict(
        resolution=resolution,
        project="alpha",
        repo=repo,
        verdict_inputs={
            "node_estimate": (2.0, "plan-fallback"),
            "capability_cache": {"configurations": []},
            "cache_status": "untracked",
        },
    )
    assert verdict["reason"] == "context-window-exceeded"
    return CompetenceLimit(verdict)


def _repository_tokens(verdict: dict[str, Any], name: str) -> int:
    """Read one file's charged tokens from the payload's own per-file census."""
    groups = verdict["context"]["inputs"]["repository_files"]
    for group in ("write_paths", "named_files"):
        for item in groups[group]:
            if item["path"].endswith(name) and item["counted"]:
                return int(item["estimated_tokens"])
    raise AssertionError(f"{name} is not a charged repository input")


def _dominant_clause(message: str) -> str:
    """Return the dominant-term clause, failing loudly when it is absent."""
    assert "dominant term:" in message, message
    return message.split("dominant term:", 1)[1]


def _assert_one_rendering(refusal: CompetenceLimit, message: str) -> None:
    """The printed line is routing's own rendering, not a sentence beside it."""
    verdict = refusal.verdict
    assert verdict["detail"] in message
    assert f"estimated {verdict['estimated_tokens']} usable input tokens" in message
    assert f"{verdict['window_tokens']} token window" in message
    assert f"shortfall {verdict['shortfall_tokens']} tokens" in message
    assert verdict["estimated_tokens"] > verdict["window_tokens"]
    assert (
        verdict["shortfall_tokens"]
        == verdict["estimated_tokens"] - verdict["window_tokens"]
    )


def test_the_printed_refusal_names_a_declared_write_path_as_dominant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The largest input is reported as a declared write path, with its tokens."""
    refusal = _refusal(
        tmp_path,
        monkeypatch,
        goal="exercise the dominant term; the brief declares small_module.py as an input.",
        write_paths=["large_module.py"],
        files={
            "large_module.py": LARGE_FILE_BYTES,
            "small_module.py": SMALL_FILE_BYTES,
        },
    )
    message = str(refusal)

    clause = _dominant_clause(message)
    assert "large_module.py" in clause
    assert "declared write path" in clause
    assert str(_repository_tokens(refusal.verdict, "large_module.py")) in clause
    _assert_one_rendering(refusal, message)


def test_the_printed_refusal_names_a_mention_as_dominant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A named file dominates its own write path, and is reported as a mention."""
    refusal = _refusal(
        tmp_path,
        monkeypatch,
        goal="exercise the dominant term; the brief declares large_module.py as an input.",
        write_paths=["small_module.py"],
        files={
            "large_module.py": LARGE_FILE_BYTES,
            "small_module.py": SMALL_FILE_BYTES,
        },
    )
    message = str(refusal)

    clause = _dominant_clause(message)
    assert "large_module.py" in clause
    assert "mention" in clause
    assert "declared write path" not in clause
    assert str(_repository_tokens(refusal.verdict, "large_module.py")) in clause
    _assert_one_rendering(refusal, message)


def test_the_printed_refusal_says_no_repository_file_dominates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Standing context alone over the window: the brief is not the problem."""
    refusal = _refusal(
        tmp_path,
        monkeypatch,
        goal="exercise the dominant term with no repository input at all",
        write_paths=[],
        files={},
        window_tokens=19_000,
    )
    message = str(refusal)

    assert "no repository file dominates the estimate" in message
    assert "dominant term:" not in message
    assert refusal.verdict["estimated_tokens"] == STANDING_INSTRUCTION_TOKENS
    _assert_one_rendering(refusal, message)
