import io

import pytest

from conftest import EXAMPLE_ENV
from hey_claude_relay.__main__ import listen_address, main
from hey_claude_relay.config import ConfigError
from hey_claude_relay.gates import verify_passphrase


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


def test_hash_passphrase_cli_reads_stdin_and_prints_only_the_hash(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("open sesame 42\n"))
    monkeypatch.delenv("ANTHROPIC_API_KEY")  # it needs no configuration at all
    assert main(["hash-passphrase"]) == 0
    out = capsys.readouterr()
    assert out.out.startswith("RELAY_PASSPHRASE_HASH=scrypt:") and out.out.count("\n") == 1
    assert "sesame" not in out.out + out.err and "forty" not in out.out + out.err
    assert verify_passphrase("open sesame forty two", out.out.strip().split("=", 1)[1])


@pytest.mark.parametrize("phrase", ["sesame", "", "   ", "never mind", "No!", "7"])
def test_hash_passphrase_cli_refuses_a_phrase_that_would_not_work(monkeypatch, capsys, phrase):
    monkeypatch.setattr("sys.stdin", io.StringIO(phrase + "\n"))
    assert main(["hash-passphrase"]) == 2
    out = capsys.readouterr()
    assert out.out == "" and "two words" in out.err


def test_hash_passphrase_on_a_terminal_asks_twice_without_echo(monkeypatch, capsys):
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    asked = []

    def fake_getpass(prompt):
        asked.append(prompt)
        return "open sesame 42"

    monkeypatch.setattr("sys.stdin", Terminal())
    monkeypatch.setattr("getpass.getpass", fake_getpass)
    assert main(["hash-passphrase"]) == 0 and asked == ["Passphrase: ", "Again: "]
    assert capsys.readouterr().out.startswith("RELAY_PASSPHRASE_HASH=scrypt:")


def test_hash_passphrase_on_a_terminal_refuses_a_mismatch(monkeypatch, capsys):
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    answers = iter(["open sesame 42", "open sesame 43"])
    monkeypatch.setattr("sys.stdin", Terminal())
    monkeypatch.setattr("getpass.getpass", lambda prompt: next(answers))
    assert main(["hash-passphrase"]) == 2
    out = capsys.readouterr()
    assert out.out == "" and "did not match" in out.err


def test_the_passphrase_is_never_a_command_line_argument(capsys):
    with pytest.raises(SystemExit):
        main(["hash-passphrase", "open sesame 42"])
