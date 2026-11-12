"""Building a codex-free codex argv spawns no process under the suite's isolation.

A dispatch test reaches a codex backend, and composing that argv asks the app
server to list the workspace's hooks so the launch can carry the inbox hook's
trust key and hash. On a node with no codex binary that exchange cannot run, so
the suite's isolation answers the listing itself. This file pins both halves:
nothing is spawned, and a trusted listing still reaches the argv.
"""

from __future__ import annotations

from reckon import _backends

_TRUST_KEY = "/<session-flags>/config.toml:post_tool_use:0:0"


def _codex_argv(worktree: str) -> list[str]:
    return _backends.dialect_for({"command": "codex"}).argv(
        command="codex",
        backend={"sandbox": "worktree-full"},
        worktree=worktree,
        working_directory=worktree,
        final_message_path=None,
        resume_session=None,
        run_directory=worktree,
    )


class _SpawnRecorder:
    """Record any spawn and refuse it: a probe here is a live account read."""

    def __init__(self) -> None:
        self.command_lines: list[list[str]] = []

    def __call__(self, argv, *args, **kwargs):  # pragma: no cover - never reached
        self.command_lines.append(list(argv))
        raise AssertionError(f"building a codex argv spawned {argv!r}")


def test_a_codex_argv_is_built_without_spawning_a_probe(tmp_path, monkeypatch):
    recorder = _SpawnRecorder()
    monkeypatch.setattr(_backends.subprocess, "Popen", recorder)

    argv = _codex_argv(str(tmp_path))

    assert recorder.command_lines == []
    assert any("hooks.PostToolUse" in word for word in argv)
    assert any("trusted_hash=" in word for word in argv)


def test_a_trusted_hook_listing_still_reaches_the_argv(tmp_path, monkeypatch):
    from reckon.hooks.install import worker_inbox_group

    command = worker_inbox_group()["hooks"][0]["command"]
    digest = "sha256:" + "b" * 64

    def listing(probe):
        return {
            "result": {
                "data": [
                    {
                        "hooks": [
                            {
                                "source": "sessionFlags",
                                "eventName": "postToolUse",
                                "handlerType": "command",
                                "command": command,
                                "key": _TRUST_KEY,
                                "currentHash": digest,
                            }
                        ]
                    }
                ]
            }
        }

    monkeypatch.setattr(_backends, "run_probe", listing)

    argv = _codex_argv(str(tmp_path))

    definition = next(word for word in argv if "hooks.PostToolUse" in word)
    assert command in definition
    trust = next(word for word in argv if "trusted_hash=" in word)
    assert _TRUST_KEY in trust
    assert digest in trust
