"""Remote ancestry reads finish or report why the remote cannot be reached."""

import json
import os
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import time

import pytest

from reckon.crew import merge


@pytest.fixture
def git_stub(tmp_path, monkeypatch):
    real_git = shutil.which("git")
    calls = tmp_path / "calls.jsonl"
    stub = tmp_path / "git"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        f"calls = Path({str(calls)!r})\n"
        "args = sys.argv[3:]\n"
        "command = os.environ.get('GIT_SSH_COMMAND', '')\n"
        "with calls.open('a') as stream:\n"
        "    stream.write(json.dumps([args, command]) + '\\n')\n"
        "if args[0] == 'config':\n"
        "    print('git@example.test:repo')\n"
        "elif args[0] == os.environ.get('SLEEP_COMMAND'):\n"
        "    time.sleep(60)\n"
        "elif args[0] == 'fetch' and os.environ.get('FETCH_ERROR') and 'Port=443' not in command:\n"
        "    print('ssh: connect to host example.test port 22: ' + os.environ['FETCH_ERROR'], file=sys.stderr)\n"
        "    sys.exit(128)\n"
        "elif args[0] == 'ls-remote':\n"
        "    if 'Port=443' not in command and os.environ.get('PORT_ERROR'):\n"
        "        print('ssh: connect to host example.test port 22: ' + "
        "os.environ['PORT_ERROR'], file=sys.stderr)\n"
        "        sys.exit(128)\n"
        "    print('a' * 40 + '\\trefs/heads/main')\n"
        "elif args[0] == 'fetch':\n"
        "    sys.exit(0)\n"
        "else:\n"
        f"    os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n"
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    return tmp_path, calls


@pytest.mark.parametrize(
    "error", ["Connection timed out", "Connection refused", "Network is unreachable"]
)
def test_failed_port_22_read_retries_over_443(git_stub, monkeypatch, error):
    repo, calls = git_stub
    monkeypatch.setenv("PORT_ERROR", error)
    head, env, detail = merge._read_remote_head(repo, "origin", "main")
    attempts = [json.loads(line) for line in calls.read_text().splitlines()]
    assert head == "a" * 40, (head, detail, attempts)
    assert len(attempts) == 2
    assert "Port=443" not in attempts[0][1]
    assert "Port=443" in attempts[1][1]
    assert "Port=443" in env["GIT_SSH_COMMAND"]


@pytest.mark.parametrize("command", ["ls-remote", "fetch"])
def test_sleeping_git_returns_unreachable_within_bound(git_stub, monkeypatch, command):
    repo, calls = git_stub
    monkeypatch.setenv("SLEEP_COMMAND", command)
    (repo / "git").write_text(
        "#!/bin/sh\n"
        f'printf \'[["%s"],""]\\n\' "$3" >> {shlex.quote(str(calls))}\n'
        'if [ "$3" = config ]; then echo git@example.test:repo; '
        'elif [ "$3" = "$SLEEP_COMMAND" ]; then exec sleep 60; '
        "else printf '%040d\\trefs/heads/main\\n' 0; fi\n"
    )
    script = """
import json, sys
from pathlib import Path
from reckon.crew import merge
merge.REMOTE_TIMEOUT_SECONDS = 0.25
merge._rev_parse = lambda repo, head: 'b' * 40
original = merge._run
def run(repo, *args, **kwargs):
    if args[0] == 'cat-file':
        return merge.subprocess.CompletedProcess(args, 1, '', '')
    return original(repo, *args, **kwargs)
merge._run = run
print('ready: ' + merge.__file__, flush=True)
print(json.dumps(merge.head_on_remote_branch(Path(sys.argv[1]), 'HEAD', branch='main')))
"""
    with subprocess.Popen(
        [sys.executable, "-c", script, str(repo)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as child:
        try:
            with selectors.DefaultSelector() as ready:
                ready.register(child.stdout, selectors.EVENT_READ)
                assert ready.select(timeout=20), "child did not finish importing"
            assert child.stdout.readline().strip() == "ready: " + merge.__file__
            started = time.monotonic()
            stdout, stderr = child.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.communicate()
            attempts = [json.loads(line) for line in calls.read_text().splitlines()]
            assert any(args[0] == command for args, _env in attempts), attempts
            pytest.fail(f"{command} blocked past the 3 second test bound")
    elapsed = time.monotonic() - started
    assert child.returncode == 0, stderr
    answer = json.loads(stdout)
    assert answer["on_branch"] is None
    assert answer["cause"] == "unreachable"
    assert "timed out" in answer["detail"]
    assert elapsed < 3
    attempts = [json.loads(line) for line in calls.read_text().splitlines()]
    assert any(args[0] == command for args, _env in attempts)
    print(f"{command}: unreachable after {elapsed:.3f}s (3s bound)")


@pytest.mark.parametrize("command", ["ls-remote", "fetch"])
def test_remote_attempt_has_connect_and_wall_clock_timeouts(
    monkeypatch, tmp_path, command
):
    observed = []

    def run(args, **kwargs):
        observed.append(kwargs)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(merge.subprocess, "run", run)
    key = str(tmp_path / "test-key")
    monkeypatch.setenv("GIT_SSH_COMMAND", f"ssh -i {shlex.quote(key)}")
    merge._run(tmp_path, command, "origin")
    options = observed[0]
    assert 0 < options["timeout"] <= 30
    ssh = shlex.split(options["env"]["GIT_SSH_COMMAND"])
    assert ssh[:3] == ["ssh", "-i", key]
    assert "ConnectTimeout=10" in ssh


@pytest.mark.parametrize("error", ["Permission denied", "Host key verification failed"])
def test_authentication_failure_does_not_select_another_route(error):
    assert not merge._port22_refused(f"example.test port 22: {error}")
    assert not merge._port22_refused("port 443: Connection timed out")


@pytest.mark.parametrize(
    "error", ["Connection timed out", "Connection refused", "No route to host"]
)
def test_failed_port_22_fetch_retries_over_443(git_stub, monkeypatch, error):
    repo, calls = git_stub
    monkeypatch.setenv("FETCH_ERROR", error)
    monkeypatch.setattr(merge, "_rev_parse", lambda repo, head: "b" * 40)
    monkeypatch.setattr(merge, "_is_ancestor", lambda repo, head, remote: True)
    original = merge._run
    present = iter([1, 0])

    def run(repo, *args, **kwargs):
        if args[0] == "cat-file":
            return subprocess.CompletedProcess(args, next(present), "", "")
        return original(repo, *args, **kwargs)

    monkeypatch.setattr(merge, "_run", run)
    answer = merge.head_on_remote_branch(repo, "HEAD", branch="main")
    assert answer["on_branch"] is True, answer
    attempts = [json.loads(line) for line in calls.read_text().splitlines()]
    fetches = [env for args, env in attempts if args[0] == "fetch"]
    assert len(fetches) == 2
    assert "Port=443" not in fetches[0]
    assert "Port=443" in fetches[1]
