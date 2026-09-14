"""部署约定：配置项清单与 config.py 一致，仓库里的部署文件、文档和防护配置齐全。"""

from __future__ import annotations

import re
from pathlib import Path

from scripts.check_deploy_readiness import CONFIG_KEYS, run_checks

ROOT = Path(__file__).resolve().parents[1]


def test_config_key_list_matches_what_config_reads():
    source = (ROOT / "text2sql" / "config.py").read_text(encoding="utf-8")
    literal = set(re.findall(r'_get\(source, "([A-Z0-9_]+)"', source))
    prefixed = {
        f"{prefix}_{suffix}"
        for prefix in ("OPENAI", "DEEPSEEK")
        for suffix in re.findall(r'_get\(source, f"\{prefix\}_([A-Z_]+)"', source)
    }

    assert literal | prefixed == set(CONFIG_KEYS)


def test_repository_passes_deploy_readiness_checks():
    failures = [f"{r.name}：{r.detail}" for r in run_checks(ROOT) if not r.ok]

    assert failures == []
