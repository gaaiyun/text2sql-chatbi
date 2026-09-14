from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def demo_db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """整个测试会话只构建一次合成演示库。"""
    from text2sql.db.demo import build_demo_database

    path = tmp_path_factory.mktemp("demo") / "znjz_demo.duckdb"
    build_demo_database(path)
    return path


@pytest.fixture(scope="session")
def demo_backend(demo_db_path: Path):
    from text2sql.db.backends import DuckDBBackend

    backend = DuckDBBackend(demo_db_path)
    yield backend
    backend.close()
