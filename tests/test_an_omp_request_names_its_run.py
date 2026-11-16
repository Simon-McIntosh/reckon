"""An omp worker's requests carry the run and session the router counts by."""

from __future__ import annotations

from pathlib import Path

import yaml

from reckon.crew import dispatch_launch

MODELS = """\
providers:
  local:
    baseUrl: http://lane.invalid/v1
    api: openai-completions
    models:
      - id: local-model
"""


def _store(tmp_path: Path, text: str = MODELS) -> Path:
    store = tmp_path / "agent"
    store.mkdir()
    (store / "models.yml").write_text(text, encoding="utf-8")
    return store


def _environment(store: Path, *, omp: bool) -> dict[str, str]:
    return dispatch_launch._worker_runtime_environment(
        {"PI_CODING_AGENT_DIR": str(store)},
        run_id="",
        manifest_path="manifest.md",
        attempt_started_at="2026-01-01T00:00:00Z",
        coordinator_session="session-one",
        claude_headers=False,
        omp_headers=omp,
    )


def test_the_run_and_session_are_written_to_every_provider(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert dispatch_launch._attribute_omp_requests(
        str(store), run_id="r-one", coordinator_session="session-one"
    )

    provider = yaml.safe_load((store / "models.yml").read_text())["providers"]["local"]
    assert provider["headers"] == {
        "X-Reckon-Run-Id": "r-one",
        "X-Reckon-Session": "session-one",
    }
    assert provider["baseUrl"] == "http://lane.invalid/v1"
    assert provider["models"] == [{"id": "local-model"}]


def test_a_header_the_operator_declared_is_kept(tmp_path: Path) -> None:
    store = _store(
        tmp_path,
        MODELS.replace(
            "    models:", "    headers:\n      X-Team: fusion\n    models:"
        ),
    )

    dispatch_launch._attribute_omp_requests(
        str(store), run_id="r-one", coordinator_session="session-one"
    )

    headers = yaml.safe_load((store / "models.yml").read_text())["providers"]["local"][
        "headers"
    ]
    assert headers["X-Team"] == "fusion"
    assert headers["X-Reckon-Run-Id"] == "r-one"


def test_a_store_that_is_absent_or_unreadable_is_left_alone(tmp_path: Path) -> None:
    assert not dispatch_launch._attribute_omp_requests(
        str(tmp_path / "missing"), run_id="r-one", coordinator_session="s"
    )
    store = _store(tmp_path, "providers: [not, a, mapping]\n")
    before = (store / "models.yml").read_text()

    assert not dispatch_launch._attribute_omp_requests(
        str(store), run_id="r-one", coordinator_session="s"
    )
    assert (store / "models.yml").read_text() == before


def test_a_linked_store_file_is_never_written_through(tmp_path: Path) -> None:
    operator = tmp_path / "operator-models.yml"
    operator.write_text(MODELS, encoding="utf-8")
    store = tmp_path / "agent"
    store.mkdir()
    (store / "models.yml").symlink_to(operator)

    assert not dispatch_launch._attribute_omp_requests(
        str(store), run_id="r-one", coordinator_session="s"
    )
    assert operator.read_text() == MODELS


def test_only_an_omp_launch_rewrites_its_store(tmp_path: Path) -> None:
    store = _store(tmp_path)

    _environment(store, omp=False)

    assert (store / "models.yml").read_text() == MODELS
