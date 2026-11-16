"""A rollout receipt and a stream's spend are read once per file identity.

The station's tick asked every terminal run for its client receipt and its
spend, and each answer decoded the same unchanged files again: a main-thread
profile put 22.8% of its samples in the receipt's line loop and a further 17.9%
in the second pass over each attempt's stream for its token count and its time
span.  Nothing in a terminal run's rollout or streams moves between ticks, so
what these readers must show is:

* the second pass over the same files decodes nothing and lists nothing;
* the second pass answers exactly what a fresh process, holding no summary at
  all, answers from the same bytes;
* a rollout that grew is decoded only past what was already read, and one that
  was truncated is read whole again, each agreeing with an uncached read.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from enum import Enum
from pathlib import Path
from typing import Any, Self

import pytest

from reckon.crew import metering, rollout

RUN_COUNT = 20
LINES_PER_FILE = 3_000
# Half the fixture's lines, the control the declaring node is measured against.
CONTROL_DECODES = 60_000
# Fixed, so a figure derived from a stamp still compares equal between passes.
NOW_SECONDS = 1_800_000_000.0
READER_MODULES = frozenset({"reckon.crew.rollout", "reckon.crew.metering"})


# ── The fixture: twenty terminal codex runs ─────────────────────────────────


def _stamp(offset_seconds: int) -> str:
    """A fixed ISO stamp, derived from the record's own position."""
    hours, rest = divmod(offset_seconds, 3_600)
    minutes, seconds = divmod(rest, 60)
    return f"2026-01-01T{hours:02d}:{minutes:02d}:{seconds:02d}Z"


def _rollout_line(index: int) -> str:
    """One rollout record, cycling through the shapes a receipt reads."""
    stamp = _stamp(index)
    if index % 10 == 0:
        # The client's cumulative reading resets part way through the file,
        # which is what the per-segment total exists to reconstruct.
        counted = index if index < LINES_PER_FILE // 2 else index - LINES_PER_FILE // 2
        payload: dict[str, Any] = {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": counted * 1_000,
                    "cached_input_tokens": counted * 400,
                    "output_tokens": counted * 200,
                },
                "last_token_usage": {
                    "input_tokens": 1_000 + index * 997,
                    "output_tokens": 700 + index,
                },
                "model_context_window": 200_000,
            },
            "rate_limits": {
                "plan_type": "pro",
                "primary": {
                    "used_percent": 42.0,
                    "window_minutes": 300,
                    "resets_at": 1_800_003_600,
                },
                "secondary": {
                    "used_percent": 12.0,
                    "window_minutes": 10_080,
                    "resets_at": 1_800_604_800,
                },
            },
        }
    elif index % 20 == 1:
        payload = {"type": "custom_tool_call", "call_id": f"call-{index}"}
    elif index % 20 == 2:
        payload = {"type": "custom_tool_call_output", "call_id": f"call-{index - 1}"}
    elif index % 20 == 5:
        payload = {"type": "reasoning", "summary": "thinking"}
    else:
        payload = {"type": "message", "text": "working"}
    return json.dumps({"timestamp": stamp, "payload": payload})


def _stream_line(index: int) -> str:
    """One codex stream record, carrying usage on every third line."""
    if index == 0:
        record: dict[str, Any] = {"type": "thread.started", "thread_id": "thread"}
    elif index % 3 == 0:
        record = {
            "type": "turn.completed",
            "timestamp": _stamp(index),
            "usage": {
                "input_tokens": 100 + index,
                "cached_input_tokens": 40 + index,
                "output_tokens": 10 + index,
                "reasoning_output_tokens": 5,
            },
        }
    elif index % 7 == 0:
        record = {"type": "turn.started", "timestamp": _stamp(index)}
    else:
        record = {"type": "item.completed", "timestamp": _stamp(index)}
    return json.dumps(record)


def _write_lines(path: Path, lines: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _session_id(index: int) -> str:
    return f"sess-{index:02d}"


def _run_id(index: int) -> str:
    return f"r-{index:02d}-terminal"


def _rollout_path(runs_root: Path, index: int) -> Path:
    return (
        runs_root
        / _run_id(index)
        / "codex-home"
        / "sessions"
        / "2026"
        / "01"
        / "01"
        / f"rollout-2026-01-01T00-00-00-{_session_id(index)}.jsonl"
    )


def _build_fixture(root: Path) -> dict[str, Any]:
    """Twenty terminal codex runs, their rollouts, streams and ledger rows."""
    runs_root = root / "reckon-home" / "crew" / "runs"
    client_sessions = root / "codex-sessions" / "sessions"
    client_sessions.mkdir(parents=True, exist_ok=True)
    rollout_lines = [_rollout_line(index) for index in range(LINES_PER_FILE)]
    stream_lines = [_stream_line(index) for index in range(LINES_PER_FILE)]
    rows: list[dict[str, Any]] = []
    for index in range(RUN_COUNT):
        run_dir = runs_root / _run_id(index)
        _write_lines(_rollout_path(runs_root, index), rollout_lines)
        _write_lines(run_dir / "stream.jsonl", stream_lines)
        if index % 10 == 0:
            # A resumed run keeps an earlier attempt's stream beside the newest
            # one, so each attempt's span is measured from its own stream and
            # the newest from the row's throughput block.
            _write_lines(run_dir / "resume-1.jsonl", stream_lines)
        rows.append(
            {
                "run_id": _run_id(index),
                "session_id": _session_id(index),
                "log_path": str(run_dir / "stream.jsonl"),
                "created_at": _stamp(0),
                "throughput": {
                    "elapsed_seconds": 100.0 + index,
                    "generation_seconds": 60.0 + index,
                },
            }
        )
    (root / "runs.json").write_text(json.dumps(rows), encoding="utf-8")
    return {
        "root": root,
        "runs_root": runs_root,
        "client_sessions": client_sessions,
        "rows": rows,
    }


@pytest.fixture()
def fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Any]]:
    built = _build_fixture(tmp_path)
    monkeypatch.setenv("RECKON_HOME", str(built["root"] / "reckon-home"))
    monkeypatch.setattr(rollout, "CLIENT_SESSIONS_DIR", built["client_sessions"])
    _clear_summaries()
    yield built
    _clear_summaries()


def _clear_summaries() -> None:
    """Drop every stored fold, so the next read decodes the bytes afresh."""
    for module in (rollout, metering):
        for name in ("_RECEIPTS", "_STREAMS", "_LISTINGS"):
            store = getattr(module, name, None)
            if isinstance(store, dict):
                store.clear()


# ── The recording stand-ins ─────────────────────────────────────────────────


class _Recorder:
    """Counts the decodes and directory listings the two readers make.

    ``Path.glob``, ``Path.iterdir``, ``os.listdir`` and ``os.scandir`` are
    wrapped alike, so a listing is counted whichever route reached the
    directory.  A decode is attributed to the module whose own code called
    ``json.loads``, which is how the readers' own parsing is told apart from
    any other library's.
    """

    def __init__(self, roots: Sequence[Path]) -> None:
        self.roots = tuple(str(Path(root).resolve()) for root in roots)
        self.decodes = 0
        self.listings = 0
        self._json_loads = json.loads
        self._listdir = os.listdir
        self._scandir = os.scandir
        self._iterdir = Path.iterdir
        self._glob = Path.glob

    def _loads(self, text: Any, *args: Any, **kwargs: Any) -> Any:
        if sys._getframe(1).f_globals.get("__name__") in READER_MODULES:
            self.decodes += 1
        return self._json_loads(text, *args, **kwargs)

    def _under(self, path: Any) -> bool:
        try:
            text = str(Path(path).resolve())
        except (TypeError, ValueError):
            return False
        return any(
            text == root or text.startswith(root + os.sep) for root in self.roots
        )

    def _count(self, path: Any) -> None:
        if self._under(path):
            self.listings += 1

    def _listdirs(self, path: Any = ".", *args: Any, **kwargs: Any) -> Any:
        self._count(path)
        return self._listdir(path, *args, **kwargs)

    def _scanfiles(self, path: Any = ".", *args: Any, **kwargs: Any) -> Any:
        self._count(path)
        return self._scandir(path, *args, **kwargs)

    def _path_wrappers(self) -> tuple[Any, Any]:
        """Plain functions: a class attribute cannot be a bound method."""
        recorder = self

        def iterdir(directory: Path) -> Any:
            recorder._count(directory)
            return recorder._iterdir(directory)

        def glob(directory: Path, *args: Any, **kwargs: Any) -> Any:
            recorder._count(directory)
            return recorder._glob(directory, *args, **kwargs)

        return iterdir, glob

    def __enter__(self) -> Self:
        json.loads = self._loads
        os.listdir = self._listdirs
        os.scandir = self._scanfiles
        Path.iterdir, Path.glob = self._path_wrappers()
        return self

    def __exit__(self, *exc_info: object) -> None:
        json.loads = self._json_loads
        os.listdir = self._listdir
        os.scandir = self._scandir
        Path.iterdir = self._iterdir
        Path.glob = self._glob


# ── Comparing answers ───────────────────────────────────────────────────────


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return f"{type(value).__name__}.{value.value}"
    if isinstance(value, Mapping):
        return {
            str(key): _plain(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if dataclasses.is_dataclass(value):
        return {
            item.name: _plain(getattr(value, item.name))
            for item in dataclasses.fields(value)
        }
    return repr(value)


def _read_all(
    rows: Sequence[Mapping[str, Any]],
    runs_root: Path,
    *,
    model_for: str | None = None,
) -> dict[str, Any]:
    """Every run's receipt and spend, as plain comparable data."""
    receipts: dict[str, Any] = {}
    spends: dict[str, Any] = {}
    for row in rows:
        run_id = str(row["run_id"])
        receipts[run_id] = _plain(
            rollout.read_rollout_receipt(
                str(row["session_id"]),
                model_identifier="gpt-5-codex" if model_for == run_id else None,
                run_id=run_id,
            )
        )
        spends[run_id] = _plain(
            metering.accumulate_run_spend(
                rows, run_id, streams_root=runs_root, now_seconds=NOW_SECONDS
            )
        )
    return {"receipts": receipts, "spends": spends}


def _fresh_values(root: Path, client_sessions: Path) -> dict[str, Any]:
    """Both readers over the fixture in a process that stored no summary."""
    rollout.CLIENT_SESSIONS_DIR = client_sessions
    rows = json.loads((root / "runs.json").read_text(encoding="utf-8"))
    return _read_all(rows, root / "reckon-home" / "crew" / "runs")


_CHILD = """
import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[3])
import test_a_receipt_and_spend_are_read_once as arm

print(
    json.dumps(
        arm._fresh_values(Path(sys.argv[1]), Path(sys.argv[2])), sort_keys=True
    )
)
"""


def _fresh_process_values(root: Path, client_sessions: Path) -> dict[str, Any]:
    """The same answers from a separate process holding no stored summary."""
    repo = Path(__file__).resolve().parents[1]
    environment = {
        key: value for key, value in os.environ.items() if key != "PYTHONPATH"
    }
    environment["PYTHONPATH"] = str(repo)
    environment["RECKON_HOME"] = str(root / "reckon-home")
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _CHILD,
            str(root),
            str(client_sessions),
            str(Path(__file__).resolve().parent),
        ],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(repo),
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


# ── The measure ────────────────────────────────────────────────────────────


def test_the_second_pass_decodes_and_lists_nothing(fixture: dict[str, Any]) -> None:
    rows, runs_root = fixture["rows"], fixture["runs_root"]
    with _Recorder([fixture["root"]]) as recorder:
        first = _read_all(rows, runs_root, model_for=str(rows[1]["run_id"]))
        first_decodes, first_listings = recorder.decodes, recorder.listings
        recorder.decodes = recorder.listings = 0
        second = _read_all(rows, runs_root, model_for=str(rows[1]["run_id"]))
        second_decodes, second_listings = recorder.decodes, recorder.listings

    # The instrument sees work that is there before it reports an absence: a
    # counter reading zero on both passes would prove nothing about the second.
    assert first_decodes >= CONTROL_DECODES, first_decodes
    assert first_listings > 0, first_listings
    assert (second_decodes, second_listings) == (0, 0)
    assert second == first


def test_the_second_pass_answers_what_a_fresh_process_answers(
    fixture: dict[str, Any],
) -> None:
    rows, runs_root = fixture["rows"], fixture["runs_root"]
    _read_all(rows, runs_root)
    cached = _read_all(rows, runs_root)

    fresh = _fresh_process_values(fixture["root"], fixture["client_sessions"])

    assert cached == fresh


def _appended_lines() -> list[str]:
    """Five records that move every figure the receipt reports."""
    base = LINES_PER_FILE
    return [
        json.dumps(
            {
                "timestamp": _stamp(base + 1),
                "payload": {"type": "custom_tool_call", "call_id": "call-late"},
            }
        ),
        json.dumps(
            {
                "timestamp": _stamp(base + 2),
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-late",
                },
            }
        ),
        json.dumps(
            {
                "timestamp": _stamp(base + 1 + 60),
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 250_000,
                            "cached_input_tokens": 1_000,
                            "output_tokens": 900,
                        },
                        "last_token_usage": {
                            "input_tokens": 300_000,
                            "output_tokens": 1_200,
                        },
                        "model_context_window": 400_000,
                    },
                    "rate_limits": {
                        "plan_type": "pro",
                        "primary": {
                            "used_percent": 43.0,
                            "window_minutes": 300,
                            "resets_at": 1_800_003_600,
                        },
                    },
                },
            }
        ),
        json.dumps(
            {
                "timestamp": _stamp(base + 4),
                "payload": {"type": "message", "text": "still working"},
            }
        ),
        json.dumps(
            {
                "timestamp": _stamp(base + 5),
                "payload": {"type": "reasoning", "summary": "more"},
            }
        ),
    ]


def test_an_appended_rollout_decodes_only_what_was_appended(
    fixture: dict[str, Any],
) -> None:
    rows, runs_root = fixture["rows"], fixture["runs_root"]
    _read_all(rows, runs_root)
    path = _rollout_path(runs_root, 3)
    appended = _appended_lines()

    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(appended) + "\n")

    with _Recorder([fixture["root"]]) as recorder:
        grown = _read_all(rows, runs_root)
        decodes = recorder.decodes

    assert decodes == len(appended), decodes
    _clear_summaries()
    assert _read_all(rows, runs_root) == grown


def test_a_truncated_rollout_is_read_whole_again(fixture: dict[str, Any]) -> None:
    rows, runs_root = fixture["rows"], fixture["runs_root"]
    _read_all(rows, runs_root)
    path = _rollout_path(runs_root, 4)
    content = path.read_bytes()
    kept = content[: len(content) // 2]
    path.write_bytes(kept)
    complete_lines = kept.count(b"\n")

    with _Recorder([fixture["root"]]) as recorder:
        reread = _read_all(rows, runs_root)
        decodes = recorder.decodes

    assert decodes == complete_lines, decodes
    _clear_summaries()
    assert _read_all(rows, runs_root) == reread
