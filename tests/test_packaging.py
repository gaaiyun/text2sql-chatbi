"""打包声明：两份依赖清单保持一致，命令行入口可导入，数据文件随包发布。

Streamlit Cloud 只认 requirements.txt，pip install 走 pyproject.toml，两处必须同步，否则部署环境和
本地安装环境会悄悄分叉。
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def requirement_lines(name: str) -> list[str]:
    lines = []
    for raw in (ROOT / name).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and not line.startswith("-r"):
            lines.append(line)
    return sorted(lines)


def test_runtime_requirements_match_pyproject():
    assert sorted(PROJECT["project"]["dependencies"]) == requirement_lines("requirements.txt")


def test_dev_requirements_match_optional_dependencies():
    dev = sorted(PROJECT["project"]["optional-dependencies"]["dev"])
    assert dev == requirement_lines("requirements-dev.txt")


def test_console_scripts_point_to_callables():
    for target in PROJECT["project"]["scripts"].values():
        module, attribute = target.split(":")
        assert callable(getattr(importlib.import_module(module), attribute))


def test_dataset_files_are_declared_as_package_data():
    patterns = PROJECT["tool"]["setuptools"]["package-data"]["text2sql"]
    # .py 是模块，随包发布；这里只管非代码的数据文件
    datasets = [
        p.relative_to(ROOT / "text2sql").as_posix()
        for p in (ROOT / "text2sql" / "datasets").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".py"
    ]

    assert datasets
    for path in datasets:
        assert any(Path(path).match(pattern) for pattern in patterns), path
