"""运行配置。

所有配置来自环境变量或 Streamlit secrets（同名键），沿用 v1 的变量名，已部署的实例无需改配置。
没有数据库主机时自动使用本地合成演示库；没有模型 Key 时只启用语义层路径。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


def is_source_checkout(root: Path = REPO_ROOT) -> bool:
    return (root / "pyproject.toml").is_file()


def default_demo_db_path(root: Path = REPO_ROOT) -> Path:
    """源码目录运行时放在仓库 data/demo 下；作为包安装时放进用户缓存目录，避免写进 site-packages。"""
    if is_source_checkout(root):
        return root / "data" / "demo" / "znjz_demo.duckdb"
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return cache / "text2sql-analysis" / "znjz_demo.duckdb"


DEFAULT_DEMO_DB_PATH = default_demo_db_path()

# 只配置了 Key 时默认走 OpenAI 官方接口；网关或其他兼容服务用 OPENAI_BASE_URL / OPENAI_MODEL 指定
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"

_PLACEHOLDER_PREFIXES = ("your-", "sk-your", "kp-your", "<")
_RATE_LIMIT = re.compile(r"^\d+\s*(/|per)\s*(second|minute|hour|day)$")


def is_placeholder(value: Any) -> bool:
    text = "" if value is None else str(value).strip()
    return not text or text.lower().startswith(_PLACEHOLDER_PREFIXES)


def _get(source: Mapping[str, Any], key: str, default: Any = None) -> Any:
    value = source.get(key)
    return default if value is None or str(value).strip() == "" else value


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _bounded_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _flag(value: Any, default: bool) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class MySQLConfig:
    host: str
    port: int = 3306
    user: str = ""
    password: str = field(default="", repr=False)
    database: str = "znjz"
    charset: str = "utf8mb4"

    @classmethod
    def from_mapping(cls, source: Mapping[str, Any]) -> MySQLConfig | None:
        host = _get(source, "DB_HOST_SCENARIO_1_3")
        if is_placeholder(host):
            return None
        return cls(
            host=str(host).strip(),
            port=_bounded_int(_get(source, "DB_PORT_SCENARIO_1_3"), 3306, 1, 65535),
            user=str(_get(source, "DB_USER_SCENARIO_1_3", "")),
            password=str(_get(source, "DB_PASSWORD_SCENARIO_1_3", "")),
            database=str(_get(source, "DB_NAME_SCENARIO_1_3", "znjz")),
        )


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    base_url: str
    api_key: str = field(repr=False)
    model: str
    temperature: float = 0.1
    timeout_s: float = 60.0
    max_agent_steps: int = 8

    @classmethod
    def from_mapping(cls, source: Mapping[str, Any]) -> LLMSettings | None:
        provider = str(_get(source, "LLM_PROVIDER", "openai_compatible")).strip().lower()
        if provider not in {"openai_compatible", "deepseek"}:
            raise ValueError(
                f"不支持的 LLM_PROVIDER：{provider}（可选 openai_compatible / deepseek）"
            )

        prefix = "DEEPSEEK" if provider == "deepseek" else "OPENAI"
        api_key = _get(source, f"{prefix}_API_KEY")
        if is_placeholder(api_key):
            return None
        default_base = (
            DEFAULT_DEEPSEEK_BASE_URL if provider == "deepseek" else DEFAULT_OPENAI_BASE_URL
        )
        default_model = DEFAULT_DEEPSEEK_MODEL if provider == "deepseek" else DEFAULT_OPENAI_MODEL
        return cls(
            provider=provider,
            base_url=str(_get(source, f"{prefix}_BASE_URL", default_base)).rstrip("/"),
            api_key=str(api_key).strip(),
            model=str(_get(source, f"{prefix}_MODEL", default_model)),
            temperature=_bounded_float(_get(source, "MODEL_TEMPERATURE"), 0.1, 0.0, 1.0),
            timeout_s=_bounded_float(_get(source, "LLM_TIMEOUT"), 60.0, 5.0, 600.0),
            max_agent_steps=_bounded_int(_get(source, "T2S_AGENT_MAX_STEPS"), 8, 2, 20),
        )

    def describe(self) -> dict[str, Any]:
        """可安全展示的配置摘要（不含 Key）。"""
        return {"provider": self.provider, "base_url": self.base_url, "model": self.model}


@dataclass(frozen=True)
class Settings:
    database: str
    demo_db_path: Path
    mysql: MySQLConfig | None
    llm: LLMSettings | None
    app_password: str = field(default="", repr=False)
    max_rows: int = 500
    max_repairs: int = 2
    query_timeout_s: float = 20.0
    cost_limit_rows: int = 50_000_000
    llm_narrative: bool = True
    allowed_origins: tuple[str, ...] = ("*",)
    rate_limit: str = "30/minute"

    @classmethod
    def from_mapping(cls, source: Mapping[str, Any] | None = None) -> Settings:
        source = source or {}
        mysql = MySQLConfig.from_mapping(source)
        requested = str(_get(source, "T2S_DATABASE", "auto")).strip().lower()
        if requested not in {"auto", "demo", "mysql"}:
            raise ValueError(f"T2S_DATABASE 只能是 auto / demo / mysql，收到：{requested}")
        if requested == "mysql" and mysql is None:
            raise ValueError("T2S_DATABASE=mysql 但未配置 DB_HOST_SCENARIO_1_3")
        database = requested if requested != "auto" else ("mysql" if mysql else "demo")

        origins = str(_get(source, "T2S_ALLOWED_ORIGINS", "*"))
        rate_limit = str(_get(source, "T2S_RATE_LIMIT", "30/minute")).strip()
        if not _RATE_LIMIT.match(rate_limit):
            raise ValueError(
                f"T2S_RATE_LIMIT 格式应为“次数/时间单位”，例如 30/minute，收到：{rate_limit}"
            )
        return cls(
            database=database,
            demo_db_path=Path(_get(source, "T2S_DEMO_DB_PATH", DEFAULT_DEMO_DB_PATH)),
            mysql=mysql,
            llm=LLMSettings.from_mapping(source),
            app_password=str(_get(source, "APP_PASSWORD", "")),
            max_rows=_bounded_int(_get(source, "T2S_MAX_ROWS"), 500, 1, 5000),
            max_repairs=_bounded_int(_get(source, "T2S_MAX_REPAIRS"), 2, 0, 5),
            query_timeout_s=_bounded_float(_get(source, "T2S_QUERY_TIMEOUT"), 20.0, 1.0, 300.0),
            cost_limit_rows=_bounded_int(
                _get(source, "T2S_COST_LIMIT"), 50_000_000, 10_000, 10**12
            ),
            llm_narrative=_flag(_get(source, "T2S_LLM_NARRATIVE"), True),
            allowed_origins=tuple(o.strip() for o in origins.split(",") if o.strip()) or ("*",),
            rate_limit=rate_limit,
        )

    @classmethod
    def from_env(cls, extra: Mapping[str, Any] | None = None) -> Settings:
        """读取 .env（不覆盖已有环境变量）后合并环境变量与额外来源（如 Streamlit secrets）。

        源码目录运行时读仓库根目录的 .env，作为包安装时读当前工作目录的 .env。
        """
        try:
            from dotenv import load_dotenv

            env_dir = REPO_ROOT if is_source_checkout() else Path.cwd()
            load_dotenv(env_dir / ".env", override=False)
        except ImportError:  # pragma: no cover - python-dotenv 是运行依赖
            pass
        merged: dict[str, Any] = dict(os.environ)
        if extra:
            merged.update({k: v for k, v in extra.items() if v is not None})
        return cls.from_mapping(merged)
