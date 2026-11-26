"""The Alpine installer: installer/build.sh makes dist/setup-hey-claude-relay.sh, a shell header with
the app appended as a base64 tarball. These tests build it and read it; none runs it, because it
needs root, apk and OpenRC. CI's alpine-install job runs it for real.
"""

import re
import shutil
import subprocess
import tarfile
from base64 import b64decode
from io import BytesIO
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "installer"
HEAD = (INSTALLER / "setup-head.sh").read_text(encoding="utf-8")
INITD = (INSTALLER / "hey-claude-relay.initd").read_text(encoding="utf-8")
WRAPPER = (INSTALLER / "hey-claude-relay.wrapper").read_text(encoding="utf-8")
MARKER = "__PAYLOAD_BELOW__"

pytestmark = pytest.mark.skipif(shutil.which("base64") is None, reason="needs base64")


def code(text: str) -> list[str]:
    """The lines that do something: no blanks, no comments."""
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("dist")
    subprocess.run(["sh", "installer/build.sh"], cwd=ROOT, env={"PATH": "/usr/bin:/bin", "DIST": str(out)}, check=True)
    return out / "setup-hey-claude-relay.sh"


def payload(installer: Path) -> tarfile.TarFile:
    text = installer.read_text(encoding="utf-8")
    encoded = text.split(f"\n{MARKER}\n", 1)[1]
    return tarfile.open(fileobj=BytesIO(b64decode(encoded)), mode="r:gz")


@pytest.mark.parametrize("script", ["setup-head.sh", "build.sh", "hey-claude-relay.initd", "hey-claude-relay.wrapper"])
def test_each_script_is_valid_sh(script):
    subprocess.run(["sh", "-n", str(INSTALLER / script)], check=True)


def test_the_built_installer_is_valid_sh_and_starts_with_the_header(built):
    subprocess.run(["sh", "-n", str(built)], check=True)
    assert built.read_text(encoding="utf-8").startswith(HEAD)
    assert built.stat().st_mode & 0o111


def test_the_payload_is_the_app_and_nothing_private(built):
    names = set(payload(built).getnames())
    for needed in (
        "pyproject.toml",
        "uv.lock",
        "LICENSE",
        "relay.example.toml",
        "src/hey_claude_relay/__main__.py",
        "src/hey_claude_relay/prompts/voice.md",
        "scripts/smoke.sh",
        "installer/hey-claude-relay.initd",
        "installer/hey-claude-relay.wrapper",
        "COMMIT",
        "UV_VERSION",
    ):
        assert f"hey-claude-relay/{needed}" in names, needed
    for name in names:
        assert name.startswith("hey-claude-relay"), name
        assert "__pycache__" not in name and not name.endswith(".pyc"), name
        base = name.rsplit("/", 1)[-1]
        assert base not in {"relay.toml", ".env", "compose.yaml", "voice.md"} or "prompts/" in name, name
        assert "/tests/" not in name and "/.git/" not in name and "/.venv/" not in name, name


def test_the_payload_pins_the_same_uv_as_the_image(built):
    image_uv = re.search(r"ghcr\.io/astral-sh/uv:([0-9.]+)@", (ROOT / "Dockerfile").read_text()).group(1)
    pinned = payload(built).extractfile("hey-claude-relay/UV_VERSION").read().decode().strip()
    assert pinned == image_uv


def test_the_payload_names_the_commit_it_was_built_from(built):
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    commit = payload(built).extractfile("hey-claude-relay/COMMIT").read().decode().strip()
    assert commit.removesuffix("-dirty") == head


def test_the_installer_needs_root_and_alpine():
    assert 'if [ "$(id -u)" != 0 ]' in HEAD and "command -v apk" in HEAD


def test_the_secrets_file_is_root_only():
    assert 'chmod 600 "$CONF"' in HEAD and 'chown root:root "$CONF"' in HEAD


@pytest.mark.parametrize(
    "key, value",
    [
        ("RELAY_CONFIG", '"$CONF_DIR/relay.toml"'),
        ("RELAY_BIND", '"$IP"'),
        ("RELAY_PORT", '"$PORT"'),
        ("ANTHROPIC_API_KEY", '""'),
        ("CF_ACCESS_CLIENT_ID", '""'),
        ("CF_ACCESS_CLIENT_SECRET", '""'),
        ("RELAY_PASSPHRASE_HASH", '""'),
    ],
)
def test_each_setting_is_added_with_its_default(key, value):
    assert re.search(rf'^add_setting {key} {re.escape(value)} ".+"$', HEAD, re.M), key


def test_a_secret_value_is_never_read_or_printed():
    for name in ("ANTHROPIC_API_KEY", "CF_ACCESS_CLIENT_SECRET", "RELAY_PASSPHRASE_HASH"):
        assert f"${name}" not in HEAD and f"${{{name}" not in HEAD, name
        assert f"conf_val {name}" not in HEAD, name


def test_add_setting_only_adds_a_missing_key(tmp_path):
    function = re.search(r"^add_setting\(\) \{.*?^\}$", HEAD, re.M | re.S).group(0)
    conf = tmp_path / "conf"
    conf.write_text("RELAY_PORT=9000\nexport ANTHROPIC_API_KEY=set-by-hand\n")
    script = f'CONF="{conf}"\n{function}\nadd_setting RELAY_PORT 8787 "port"\nadd_setting RELAY_BIND 10.0.0.2 "bind"\n'
    script += 'add_setting ANTHROPIC_API_KEY "" "key"\n'
    script += 'echo "added:$ADDED"\n'
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "added: RELAY_BIND"
    text = conf.read_text()
    assert "RELAY_PORT=9000" in text and "RELAY_PORT=8787" not in text and "RELAY_BIND=10.0.0.2" in text
    assert "\nANTHROPIC_API_KEY=" not in text  # an exported line counts: an empty one after it would win


def test_an_existing_relay_toml_is_never_replaced():
    copy = next(line for line in code(HEAD) if "relay.example.toml" in line and "relay.toml" in line)
    assert copy.startswith('[ -f "$CONF_DIR/relay.toml" ] ||'), copy


def test_the_service_starts_only_when_check_config_passes():
    lines = code(HEAD)
    start = [i for i, line in enumerate(lines) if line.startswith("rc-service hey-claude-relay start")]
    assert len(start) == 1
    guard = next(lines[j] for j in range(start[0], -1, -1) if lines[j].startswith("if "))
    assert "check-config" in guard, guard


def test_a_failed_dependency_install_puts_the_old_app_back():
    sync = next(line for line in code(HEAD) if " sync " in line and "--locked" in line)
    assert "--no-dev" in sync and "--no-editable" in sync
    assert re.search(r"^rollback\(\) \{", HEAD, re.M) and "|| rollback" in HEAD


def test_uninstall_keeps_the_configuration():
    block = re.search(r'if \[ "\$\{1:-\}" = "--uninstall" \]; then\n(.*?)\n  exit 0\nfi', HEAD, re.S).group(1)
    assert 'rm -rf "$APP_DIR" "$RUNTIME_DIR"' in block
    removals = [line for line in code(block) if line.startswith("rm ")]
    assert removals and all("$CONF" not in line and "/etc/hey-claude-relay" not in line for line in removals)


def test_the_init_script_runs_the_relay_unprivileged_under_supervision():
    for needle in (
        'command="/opt/hey-claude-relay/.venv/bin/python"',
        'command_args="-m hey_claude_relay serve"',
        'command_user="relay:relay"',
        'supervisor="supervise-daemon"',
        'output_log="/var/log/hey-claude-relay.log"',
        "need net",
    ):
        assert needle in INITD, needle


def test_the_init_script_passes_every_setting_in_the_conf_file():
    # A server's token_env names a variable the installer cannot know, so the whole file is exported.
    assert "set -a" in code(INITD) and "set +a" in code(INITD)
    assert '. "/etc/conf.d/${RC_SVCNAME}"' in INITD
    assert "RELAY_COMMIT" in INITD


def test_the_wrapper_runs_the_installed_relay_with_its_settings():
    lines = code(WRAPPER)
    assert lines[-1] == 'exec /opt/hey-claude-relay/.venv/bin/python -m hey_claude_relay "$@"'
    assert any("set -a" in line and ". /etc/conf.d/hey-claude-relay" in line for line in lines)
