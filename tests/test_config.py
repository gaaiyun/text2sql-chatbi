from __future__ import annotations

import pytest

from text2sql.config import LLMSettings, MySQLConfig, Settings, default_demo_db_path


def test_defaults_resolve_to_offline_demo():
    settings = Settings.from_mapping({})

    assert settings.database == "demo"
    assert settings.mysql is None
    assert settings.llm is None
    assert settings.max_rows == 500
    assert settings.max_repairs == 2
    assert settings.demo_db_path.name == "znjz_demo.duckdb"


def test_mysql_config_uses_v1_secret_names():
    config = MySQLConfig.from_mapping(
        {
            "DB_HOST_SCENARIO_1_3": "db.example",
            "DB_PORT_SCENARIO_1_3": "3307",
            "DB_NAME_SCENARIO_1_3": "znjz",
            "DB_USER_SCENARIO_1_3": "reader",
            "DB_PASSWORD_SCENARIO_1_3": "secret",
        }
    )

    assert config == MySQLConfig(
        host="db.example", port=3307, user="reader", password="secret", database="znjz"
    )


@pytest.mark.parametrize("host", ["", "your-db-host", None])
def test_mysql_config_ignores_placeholder_hosts(host):
    assert MySQLConfig.from_mapping({"DB_HOST_SCENARIO_1_3": host}) is None


def test_auto_database_switches_to_mysql_when_host_is_configured():
    settings = Settings.from_mapping(
        {"DB_HOST_SCENARIO_1_3": "db.example", "DB_USER_SCENARIO_1_3": "u"}
    )
    assert settings.database == "mysql"


def test_explicit_demo_overrides_configured_mysql():
    settings = Settings.from_mapping({"T2S_DATABASE": "demo", "DB_HOST_SCENARIO_1_3": "db.example"})
    assert settings.database == "demo"


def test_explicit_mysql_without_host_is_an_error():
    with pytest.raises(ValueError, match="DB_HOST_SCENARIO_1_3"):
        Settings.from_mapping({"T2S_DATABASE": "mysql"})


def test_llm_settings_from_openai_compatible_env():
    settings = LLMSettings.from_mapping(
        {
            "LLM_PROVIDER": "openai_compatible",
            "OPENAI_BASE_URL": "https://relay.example/v1",
            "OPENAI_API_KEY": "sk-real-looking",
            "OPENAI_MODEL": "grok-4.5",
            "MODEL_TEMPERATURE": "0.2",
        }
    )

    assert settings.provider == "openai_compatible"
    assert settings.base_url == "https://relay.example/v1"
    assert settings.model == "grok-4.5"
    assert settings.temperature == 0.2
    assert "sk-real-looking" not in repr(settings)
    assert "api_key" not in settings.describe()


def test_llm_settings_deepseek_provider():
    settings = LLMSettings.from_mapping(
        {
            "LLM_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": "ds-key",
            "DEEPSEEK_MODEL": "deepseek-v4-flash",
        }
    )

    assert settings.provider == "deepseek"
    assert settings.base_url == "https://api.deepseek.com"
    assert settings.model == "deepseek-v4-flash"


@pytest.mark.parametrize("key", ["", "your-openai-compatible-api-key-here", "sk-your-key"])
def test_llm_settings_absent_for_placeholder_keys(key):
    assert LLMSettings.from_mapping({"OPENAI_API_KEY": key}) is None


def test_unknown_llm_provider_is_rejected():
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        LLMSettings.from_mapping({"LLM_PROVIDER": "mystery", "OPENAI_API_KEY": "k"})


def test_numeric_limits_are_clamped():
    settings = Settings.from_mapping(
        {"T2S_MAX_ROWS": "999999", "T2S_MAX_REPAIRS": "-3", "T2S_QUERY_TIMEOUT": "abc"}
    )

    assert settings.max_rows == 5000
    assert settings.max_repairs == 0
    assert settings.query_timeout_s == 20.0


def test_allowed_origins_and_password():
    settings = Settings.from_mapping(
        {"T2S_ALLOWED_ORIGINS": "https://a.example, https://b.example", "APP_PASSWORD": "pw"}
    )

    assert settings.allowed_origins == ("https://a.example", "https://b.example")
    assert settings.app_password == "pw"


def test_demo_db_lives_in_repo_data_dir_for_source_checkouts(tmp_path):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")

    assert default_demo_db_path(tmp_path) == tmp_path / "data" / "demo" / "znjz_demo.duckdb"


def test_demo_db_goes_to_user_cache_when_installed_as_package(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    expected = tmp_path / "cache" / "text2sql-analysis" / "znjz_demo.duckdb"
    assert default_demo_db_path(tmp_path / "site-packages") == expected


def test_rate_limit_uses_limits_syntax_and_rejects_garbage():
    assert Settings.from_mapping({}).rate_limit == "30/minute"
    assert Settings.from_mapping({"T2S_RATE_LIMIT": "5/second"}).rate_limit == "5/second"
    with pytest.raises(ValueError, match="T2S_RATE_LIMIT"):
        Settings.from_mapping({"T2S_RATE_LIMIT": "lots"})


def test_llm_narrative_flag():
    assert Settings.from_mapping({"T2S_LLM_NARRATIVE": "false"}).llm_narrative is False
    assert Settings.from_mapping({}).llm_narrative is True
