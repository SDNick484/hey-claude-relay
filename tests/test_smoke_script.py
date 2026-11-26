"""scripts/smoke.sh against a real relay on loopback: the one test that opens a socket.

The relay under test has a scripted Claude and no MCP servers, so nothing leaves this machine.
"""

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from conftest import load_toml
from fakes import FakeClaude, say
from hey_claude_relay.app import create_app

pytestmark = pytest.mark.skipif(shutil.which("curl") is None, reason="needs curl")
SCRIPT = ["sh", "scripts/smoke.sh"]


def _serve(toml: str):
    cfg, secrets = load_toml(toml)
    claude = FakeClaude(say("Smoke test heard."), say("Still here."))
    server = uvicorn.Server(
        uvicorn.Config(create_app(cfg, secrets, claude=claude), host="127.0.0.1", port=0, log_config=None)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "the test relay did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def local_relay_url():
    yield from _serve("")


@pytest.fixture
def local_profile_relay_url():
    yield from _serve('[[profile]]\nname = "home"\n\n[[profile]]\nname = "shared"\n')


def run(env):
    return run_args([], env)


def run_args(args, env):
    return subprocess.run(
        SCRIPT + list(args), env={"PATH": os.environ["PATH"], **env}, capture_output=True, text=True, timeout=60
    )


def test_smoke_script_passes_against_a_local_relay(local_relay_url):
    out = run({"RELAY_URL": local_relay_url, "RELAY_SMOKE_LOCAL": "1"})
    assert out.returncode == 0, out.stdout + out.stderr
    lines = out.stdout.splitlines()
    assert [line.split()[0] for line in lines] == ["ok"] * 7 + ["All"], out.stdout
    assert "Smoke test heard." in out.stdout  # it shows the reply, so a person can see it is sensible


def test_a_trailing_slash_on_the_url_is_fine(local_relay_url):
    assert run({"RELAY_URL": local_relay_url + "/", "RELAY_SMOKE_LOCAL": "1"}).returncode == 0


def test_without_the_local_flag_a_relay_that_answers_strangers_fails_the_check(local_relay_url):
    # A deployed relay must refuse a turn that carries no credentials. This local one does not
    # (it has no [access] table), which is exactly what the check is there to catch.
    out = run({"RELAY_URL": local_relay_url})
    assert out.returncode != 0 and "without credentials" in out.stdout + out.stderr


def test_smoke_script_fails_loudly_when_the_relay_is_down():
    out = run({"RELAY_URL": "http://127.0.0.1:9"})
    assert out.returncode != 0 and "healthz" in out.stdout + out.stderr
    assert "ok" not in out.stdout


def test_smoke_script_needs_the_url():
    out = run({})
    assert out.returncode != 0 and "RELAY_URL" in out.stderr


def test_smoke_script_never_prints_the_secret(local_relay_url):
    env = {
        "RELAY_URL": local_relay_url,
        "RELAY_SMOKE_LOCAL": "1",
        "CF_ACCESS_CLIENT_ID": "smoke-client.access",
        "CF_ACCESS_CLIENT_SECRET": "s3cret-value-that-must-not-appear",
    }
    out = run(env)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "s3cret-value" not in out.stdout + out.stderr


def test_smoke_script_is_plain_posix_sh():
    text = Path("scripts/smoke.sh").read_text()
    assert text.startswith("#!/bin/sh\n") and "set -eu" in text
    for bashism in ("[[", "function ", "local ", "<<<", "$'", "pipefail", "jq "):
        assert bashism not in text, bashism
    assert os.access("scripts/smoke.sh", os.X_OK)


@pytest.mark.parametrize("secret", ['has"quote', "has\\backslash"])
def test_a_secret_curl_could_not_carry_whole_is_refused(local_relay_url, secret):
    env = {"RELAY_URL": local_relay_url, "RELAY_SMOKE_LOCAL": "1", "CF_ACCESS_CLIENT_ID": "x.access"}
    out = run({**env, "CF_ACCESS_CLIENT_SECRET": secret})
    assert out.returncode != 0 and "CF_ACCESS_CLIENT_SECRET" in out.stderr and secret not in out.stdout + out.stderr


def test_the_script_cleans_up_when_interrupted():
    text = Path("scripts/smoke.sh").read_text()
    assert "trap 'exit 130' INT TERM HUP" in text  # dash and ash do not run the EXIT trap on a signal otherwise


def test_profile_option_is_sent(local_profile_relay_url):
    out = run_args(["--profile", "shared"], {"RELAY_URL": local_profile_relay_url, "RELAY_SMOKE_LOCAL": "1"})
    assert out.returncode == 0, out.stdout + out.stderr
    assert [line.split()[0] for line in out.stdout.splitlines()] == ["ok"] * 7 + ["All"], out.stdout


def test_a_profile_the_relay_does_not_have_fails(local_profile_relay_url):
    out = run_args(["--profile", "office"], {"RELAY_URL": local_profile_relay_url, "RELAY_SMOKE_LOCAL": "1"})
    assert out.returncode != 0 and "HTTP 403" in out.stderr, out.stdout + out.stderr


@pytest.mark.parametrize("args", [["--profile"], ["--profile", 'x"y'], ["--profile", "Office"], ["--bogus"]])
def test_bad_arguments_are_usage_errors(args):
    out = run_args(args, {"RELAY_URL": "http://127.0.0.1:9"})
    assert out.returncode == 2 and "usage" in out.stderr.lower(), out.stdout + out.stderr
    assert "ok" not in out.stdout
