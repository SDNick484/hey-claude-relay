import pytest

from conftest import EXAMPLE_ENV
from hey_claude_relay.__main__ import listen_address, main
from hey_claude_relay.config import ConfigError


@pytest.fixture(autouse=True)
def example_environment(monkeypatch):
    for k, v in EXAMPLE_ENV.items():
        monkeypatch.setenv(k, v)


def test_check_config_exit_codes(tmp_path, capsys):
    bad = tmp_path / "relay.toml"
    bad.write_text('modle = "x"\n')
    assert main(["check-config", "--config", str(bad)]) == 2
    assert "modle" in capsys.readouterr().err
    assert main(["check-config", "--config", "relay.example.toml"]) == 0


def test_config_path_comes_from_the_environment(tmp_path, monkeypatch, capsys):
    good = tmp_path / "elsewhere.toml"
    good.write_text("")
    monkeypatch.setenv("RELAY_CONFIG", str(good))
    assert main(["check-config"]) == 0
    assert "elsewhere.toml" in capsys.readouterr().out


def test_listen_address_defaults_and_overrides():
    assert listen_address(None, None, {}) == ("127.0.0.1", 8787)
    assert listen_address(None, None, {"RELAY_BIND": "0.0.0.0", "RELAY_PORT": "9000"}) == ("0.0.0.0", 9000)
    assert listen_address("192.0.2.5", 8080, {"RELAY_BIND": "0.0.0.0", "RELAY_PORT": "9000"}) == ("192.0.2.5", 8080)


@pytest.mark.parametrize("port", ["eighty", "0", "70000"])
def test_a_bad_port_is_a_config_error(port):
    with pytest.raises(ConfigError, match="RELAY_PORT"):
        listen_address(None, None, {"RELAY_PORT": port})


def test_check_config_reads_the_prompt_file_too(tmp_path, capsys):
    cfg = tmp_path / "relay.toml"
    cfg.write_text('prompt_file = "voice.md"\n')
    assert main(["check-config", "--config", str(cfg)]) == 2
    assert "voice.md" in capsys.readouterr().err
    (tmp_path / "voice.md").write_text("Be brief.\n")
    assert main(["check-config", "--config", str(cfg)]) == 0


def test_check_config_needs_the_api_key(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    cfg = tmp_path / "relay.toml"
    cfg.write_text("")
    assert main(["check-config", "--config", str(cfg)]) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
