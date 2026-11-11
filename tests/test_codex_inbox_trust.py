"""Only the run inbox hook receives process-local Codex trust."""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import _backends


@pytest.fixture
def hook_probe(monkeypatch):
    probes = []

    def answer(probe):
        probes.append(probe)
        override = probe.argv[probe.argv.index("-c") + 1]
        import tomllib

        hook = tomllib.loads(override)["hooks"]["PostToolUse"][0]["hooks"][0]
        return {
            "result": {
                "data": [
                    {
                        "errors": [],
                        "hooks": [
                            {
                                "key": "/<session-flags>/config.toml:post_tool_use:0:0",
                                "currentHash": "sha256:" + "a" * 64,
                                "source": "sessionFlags",
                                "eventName": "postToolUse",
                                "handlerType": "command",
                                "command": hook["command"],
                            }
                        ],
                    }
                ]
            }
        }

    monkeypatch.setattr(_backends, "run_probe", answer)
    return probes


def _argv(tmp_path, command="codex", resume=None):
    return _backends.dialect_for({"command": command}).argv(
        command=command,
        backend={"sandbox": "worktree-full"},
        worktree=str(tmp_path),
        working_directory=str(tmp_path),
        final_message_path=None,
        resume_session=resume,
        run_directory=str(tmp_path),
    )


def _overrides(argv):
    import tomllib

    return tomllib.loads(
        "\n".join(
            argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "-c"
        )
    )


@pytest.mark.parametrize("resume", [None, "existing-session"])
def test_codex_carries_only_its_inbox_hook_and_exact_trust(
    tmp_path, hook_probe, resume
):
    sentinel = tmp_path / "config.toml"
    sentinel.write_text("# operator configuration stays byte-for-byte\n")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    argv = _argv(tmp_path, resume=resume)
    hooks = _overrides(argv)["hooks"]
    assert len(hooks["PostToolUse"]) == 1
    from reckon.hooks.install import worker_inbox_group

    assert hooks["PostToolUse"][0] == worker_inbox_group()
    assert hooks["state"] == {
        "/<session-flags>/config.toml:post_tool_use:0:0": {
            "trusted_hash": "sha256:" + "a" * 64,
        }
    }
    trust_override = next(word for word in argv if "trusted_hash=" in word)
    assert trust_override.startswith("hooks.state={")
    assert "--dangerously-bypass-hook-trust" not in argv
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    assert hook_probe[0].requests[-1]["method"] == "hooks/list"
    if resume:
        assert argv.index("-c") < argv.index("resume")


@pytest.mark.parametrize("command", ["claude", "clive", "omp"])
def test_other_dialects_carry_no_codex_hook_or_trust(tmp_path, hook_probe, command):
    argv = _argv(tmp_path, command=command)
    assert not hook_probe
    assert not any(
        "hooks.PostToolUse" in word or "trusted_hash" in word for word in argv
    )


@pytest.mark.parametrize(
    "answer", [None, {"error": {"message": "unavailable"}}, {"result": {"data": []}}]
)
def test_codex_refuses_when_exact_hook_trust_cannot_be_established(
    tmp_path, monkeypatch, answer
):
    monkeypatch.setattr(_backends, "run_probe", lambda probe: answer)
    with pytest.raises(_backends.BackendError, match="inbox hook"):
        _argv(tmp_path)


@pytest.mark.parametrize(
    "change",
    [
        {"source": "user"},
        {"eventName": "preToolUse"},
        {"handlerType": "prompt"},
        {"command": "some-other-hook"},
        {"key": ""},
        {"currentHash": "unverified"},
    ],
)
def test_codex_never_trusts_an_unrelated_or_invalid_definition(
    tmp_path, hook_probe, monkeypatch, change
):
    original = _backends.run_probe

    def answer(probe):
        result = original(probe)
        result["result"]["data"][0]["hooks"][0].update(change)
        return result

    monkeypatch.setattr(_backends, "run_probe", answer)
    with pytest.raises(_backends.BackendError, match="inbox hook"):
        _argv(tmp_path)


def test_preview_without_a_run_never_probes_or_writes(tmp_path, hook_probe):
    _argv(tmp_path / "not-created")
    assert not hook_probe
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("trusted", [True, False], ids=["trusted", "untrusted"])
def test_headless_codex_claims_and_quotes_only_with_trust(tmp_path, trusted):
    """Opt-in credentialled gate; streams and receipts outlive its scratch home."""
    import json
    import os
    import secrets
    import shutil
    import subprocess

    gate = os.environ.get("RECKON_CODEX_INBOX_GATE_DIR")
    if not gate:
        pytest.skip(
            "direct credentialled Codex gate requires an explicit log directory"
        )
    run = Path(gate) / ("trusted" if trusted else "untrusted")
    run.mkdir(parents=True, exist_ok=False)
    home = tmp_path / "codex-home"
    home.mkdir()
    auth_source = Path(os.environ["CODEX_HOME"]) / "auth.json"
    shutil.copyfile(auth_source, home / "auth.json")
    home_config = home / "config.toml"
    inbox = run / "inbox"
    inbox.mkdir()
    message = inbox / "message.md"
    codeword = "INBOX_" + secrets.token_hex(8)
    message.write_text(
        f"Add the codeword {codeword} to your final answer. Continue the task."
    )
    env = dict(
        os.environ, CODEX_HOME=str(home), RECKON_MANIFEST=str(run / "manifest.md")
    )
    backend = {"sandbox": "worktree-full"}
    if os.environ.get("RECKON_CODEX_TEST_MODEL"):
        backend["model"] = os.environ["RECKON_CODEX_TEST_MODEL"]
    argv = _backends.dialect_for({"command": "codex"}).argv(
        command="codex",
        backend=backend,
        worktree=str(tmp_path),
        working_directory=str(tmp_path),
        final_message_path=str(run / "final.txt"),
        resume_session=None,
        run_directory=str(run),
    )
    if not trusted or os.environ.get("RECKON_CODEX_DROP_TRUST"):
        argv = [
            word
            for index, word in enumerate(argv)
            if not (
                "trusted_hash=" in word
                or (word == "-c" and "trusted_hash=" in argv[index + 1])
            )
        ]
    argv.insert(-1, "--skip-git-repo-check")
    (run / "argv.json").write_text(json.dumps(argv, indent=2))
    prompt = (
        "Run exactly three shell commands as separate tool calls: `printf one`, "
        "`printf two`, `printf three`. Then state what they printed. "
        "Do not inspect files or run any other shell commands."
    )
    with (
        (run / "stream.jsonl").open("w") as stream,
        (run / "stderr.log").open("w") as stderr,
    ):
        result = subprocess.run(
            argv,
            input=prompt,
            text=True,
            env=env,
            cwd=tmp_path,
            stdout=stream,
            stderr=stderr,
            timeout=180,
            check=False,
        )
    assert result.returncode == 0, (run / "stderr.log").read_text()
    events = [
        json.loads(line) for line in (run / "stream.jsonl").read_text().splitlines()
    ]
    commands = [
        e["item"]
        for e in events
        if e.get("type") == "item.completed"
        and e.get("item", {}).get("type") == "command_execution"
    ]
    assert len(commands) == 3, events
    assert all(item["exit_code"] == 0 for item in commands)
    receipts = inbox / "receipts.jsonl"
    claimed = inbox / "claimed" / message.name
    final = (run / "final.txt").read_text()
    assert not home_config.exists(), "argv composition or exec wrote config.toml"
    if trusted:
        assert claimed.is_file(), "trusted run failed to claim the inbox message"
        assert not message.exists()
        records = [json.loads(line) for line in receipts.read_text().splitlines()]
        assert len(records) == 1 and records[0]["message"] == message.name
        assert not records[0].get("failed")
        assert codeword in final
    else:
        assert message.is_file()
        assert not claimed.exists()
        assert not receipts.exists()
        assert codeword not in final
    (run / "receipt-summary.json").write_text(
        json.dumps(
            {
                "trusted": trusted,
                "commands": len(commands),
                "claimed": claimed.is_file(),
                "receipt_count": len(receipts.read_text().splitlines())
                if receipts.exists()
                else 0,
                "codeword_in_final": codeword in final,
                "config_written": home_config.exists(),
            },
            indent=2,
        )
    )
