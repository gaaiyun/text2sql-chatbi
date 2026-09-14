"""敏感信息扫描：能认出真实的泄露，也不把示例值当成问题；仓库本身必须扫描通过。"""

from __future__ import annotations

import pytest

from scripts.check_security import scan_repository, scan_text


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        # 样本本身就是“泄露”，行尾的标记让仓库扫描跳过这些源码行；传给 scan_text 的字符串里不含标记
        ("DB_HOST=1.1.1.1  # security: allow 测试样本", None),
        ("- Host：`203.0.113.9`", None),  # 文档保留网段不是公网
        ("host = '8.8.8.8'", "公网 IP"),  # security: allow
        ("OPENAI_API_KEY=sk-" + "proj-abcdefghijklmnopqrstuvwxyz123456", "OpenAI 风格 Key"),
        ("CREATE DEFINER=`reader`@`%` VIEW v AS SELECT 1", "MySQL DEFINER"),  # security: allow
        ('password = "hunter2-real"', "写死的口令或密钥"),
        ("-----BEGIN OPENSSH PRIVATE KEY-----", "私钥"),  # security: allow
    ],
)
def test_real_leaks_are_found(line, kind):
    kinds = [finding.kind for finding in scan_text("config.py", line)]

    assert kinds == ([kind] if kind else [])


@pytest.mark.parametrize(
    "line",
    [
        "uvicorn.run(app, host='127.0.0.1', port=8000)",
        "server_name='0.0.0.0'",
        "DB_HOST_SCENARIO_1_3=your-db-host",
        'DB_PASSWORD_SCENARIO_1_3 = "your-db-password"',
        "DASHSCOPE_API_KEY=sk-your-dashscope-api-key-here",
        'APP_PASSWORD = "change-me"',
        "Pyodide 314.0.7 · DuckDB 1.5.1 · sqlglot 30.18.0",
        'password: "<数据库密码>"',
    ],
)
def test_placeholders_and_local_addresses_are_ignored(line):
    assert scan_text("docs/example.md", line) == []


def test_fake_passwords_in_tests_are_not_reported():
    assert scan_text("tests/test_api.py", 'make_client(APP_PASSWORD="s3cret")') == []


def test_repository_has_no_findings():
    assert scan_repository() == []
