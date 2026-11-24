from pathlib import Path

import pytest

from conftest import EXAMPLE_ENV
from hey_claude_relay.config import ConfigError, load_config, prompt_text


def load(tmp_path, text, env=None):
    p = tmp_path / "relay.toml"
    p.write_text(text)
    return load_config(p, env or {})


def test_defaults_are_the_spec_values(tmp_path):
    cfg, _ = load(tmp_path, "")
    assert (cfg.model, cfg.idle_minutes, cfg.max_rounds, cfg.turn_seconds) == ("claude-sonnet-5-5", 15, 8, 45)


def test_unknown_key_stops_startup(tmp_path):
    with pytest.raises(ConfigError, match="modle"):
        load(tmp_path, 'modle = "x"\n')


def test_access_needs_an_audience_and_a_caller(tmp_path):
    with pytest.raises(ConfigError, match="audiences"):
        load(
            tmp_path,
            '[access]\nteam_domain = "example.cloudflareaccess.com"\naudiences = []\nservice_tokens = ["a.access"]\n',
        )


def test_prompt_file_is_relative_to_the_config(tmp_path):
    cfg, _ = load(tmp_path, 'prompt_file = "voice.md"\n')
    assert cfg.prompt_file == tmp_path / "voice.md"


def test_secrets_stay_out_of_repr(tmp_path):
    _, secrets = load(tmp_path, "", {"ANTHROPIC_API_KEY": "sk-test-123", "CF_ACCESS_CLIENT_SECRET": "hunter2"})
    assert secrets.anthropic_api_key == "sk-test-123"
    assert "sk-test-123" not in repr(secrets) and "hunter2" not in repr(secrets)


def test_example_config_loads():
    cfg, _ = load_config(Path("relay.example.toml"), EXAMPLE_ENV)
    assert cfg.access is not None and cfg.access.team_domain == "example.cloudflareaccess.com"


def test_a_missing_file_says_which(tmp_path):
    with pytest.raises(ConfigError, match="relay.toml"):
        load_config(tmp_path / "relay.toml", {})


def test_broken_toml_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="relay.toml"):
        load(tmp_path, "model = \n")


def test_a_bad_value_names_the_key_and_not_the_value(tmp_path):
    with pytest.raises(ConfigError, match="max_rounds") as e:
        load(tmp_path, 'max_rounds = "s3cret-looking-value"\n')
    assert "s3cret-looking-value" not in str(e.value)


def test_blank_environment_values_count_as_unset(tmp_path):
    _, secrets = load(tmp_path, "", {"ANTHROPIC_API_KEY": "  "})
    assert secrets.anthropic_api_key is None


def test_without_prompt_file_the_bundled_prompt_is_used(tmp_path):
    cfg, _ = load(tmp_path, "")
    assert cfg.prompt_file is None
    assert "read aloud" in prompt_text(cfg)


def test_a_custom_prompt_file_is_read(tmp_path):
    (tmp_path / "voice.md").write_text("Speak like a pirate.\n")
    cfg, _ = load(tmp_path, 'prompt_file = "voice.md"\n')
    assert prompt_text(cfg) == "Speak like a pirate."


@pytest.mark.parametrize("content", [None, "", "  \n"])
def test_a_missing_or_empty_custom_prompt_is_a_config_error(tmp_path, content):
    if content is not None:
        (tmp_path / "voice.md").write_text(content)
    cfg, _ = load(tmp_path, 'prompt_file = "voice.md"\n')
    with pytest.raises(ConfigError, match="voice.md"):
        prompt_text(cfg)


@pytest.mark.parametrize(
    "key, table",
    [
        ("audiences", 'audiences = [""]\nservice_tokens = ["a.access"]\n'),
        ("service_tokens", 'audiences = ["aud-1"]\nservice_tokens = ["  "]\n'),
        ("service_tokens", 'audiences = ["aud-1"]\nservice_tokens = ["a.access", ""]\n'),
    ],
)
def test_a_blank_entry_in_an_allow_list_is_refused(tmp_path, key, table):
    # A blank entry must never become "anything without a name is allowed".
    with pytest.raises(ConfigError, match=key):
        load(tmp_path, '[access]\nteam_domain = "example.cloudflareaccess.com"\n' + table)


def test_allow_list_entries_are_trimmed(tmp_path):
    table = 'audiences = [" aud-1 "]\nservice_tokens = ["a.access "]\n'
    cfg, _ = load(tmp_path, '[access]\nteam_domain = "example.cloudflareaccess.com"\n' + table)
    assert cfg.access.audiences == ("aud-1",) and cfg.access.service_tokens == ("a.access",)
