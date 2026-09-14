"""命令行入口：真实演示库，离线运行（无模型、无 MySQL）。"""

from __future__ import annotations

import io
import json

import pytest

from text2sql.cli import main


@pytest.fixture
def offline_env(monkeypatch, demo_db_path):
    """隔离开发者本地 .env：已存在的空变量不会被 python-dotenv 回填，配置层也视为未配置。"""
    for key in (
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "LLM_PROVIDER",
        "DB_HOST_SCENARIO_1_3",
        "APP_PASSWORD",
    ):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("T2S_DATABASE", "demo")
    monkeypatch.setenv("T2S_DEMO_DB_PATH", str(demo_db_path))
    return demo_db_path


def test_ask_prints_markdown_report(offline_env, capsys):
    code = main(["ask", "广州市存续企业有多少家"])
    out = capsys.readouterr().out

    assert code == 0
    assert "## 结论" in out
    assert "```sql" in out


def test_ask_json_output_is_machine_readable(offline_env, capsys):
    code = main(["ask", "资质状态分布", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "answered"
    assert payload["planner"] == "semantic"
    assert payload["rows"]


def test_ask_stream_prints_node_progress_to_stderr(offline_env, capsys):
    code = main(["ask", "资质状态分布", "--stream"])
    captured = capsys.readouterr()

    assert code == 0
    assert "[understand]" in captured.err
    assert "[finalize]" in captured.err
    assert "## 结论" in captured.out


def test_unanswered_question_exits_with_1(offline_env, capsys):
    assert main(["ask", "今天天气怎么样"]) == 1


def test_llm_planner_without_model_is_a_usage_error(offline_env, capsys):
    code = main(["ask", "资质状态分布", "--planner", "llm"])

    assert code == 2
    assert "OPENAI_API_KEY" in capsys.readouterr().err


def test_chat_keeps_context_between_turns(offline_env, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("广州市存续企业有多少家\n那深圳呢\n/exit\n"))

    code = main(["chat"])
    out = capsys.readouterr().out

    assert code == 0
    assert "已理解为：深圳" in out


def test_chat_new_command_starts_a_fresh_thread(offline_env, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("广州市存续企业有多少家\n/new\n那深圳呢\n"))

    assert main(["chat"]) == 0
    out = capsys.readouterr().out
    assert "已开始新会话" in out
    assert "还没有上一轮问题" in out


def test_eval_subset_passes_gate_and_writes_outputs(offline_env, tmp_path, capsys):
    report = tmp_path / "EVALUATION.md"
    report.write_text("# 评测\n\n手写说明\n", encoding="utf-8")
    output = tmp_path / "eval.json"

    code = main(
        [
            "eval",
            "--ids",
            "b01,b02,r01",
            "--gate",
            "--write-report",
            "--report",
            str(report),
            "--json",
            str(output),
        ]
    )
    out = capsys.readouterr().out
    document = report.read_text(encoding="utf-8")
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert code == 0
    assert "[PASS]" in out
    assert "手写说明" in document
    assert "<!-- EVAL:semantic:BEGIN -->" in document
    assert payload["summary"]["total"] == 3
    assert [o["id"] for o in payload["outcomes"]] == ["b01", "b02", "r01"]


def test_eval_gate_failure_exits_with_1(offline_env, capsys):
    code = main(["eval", "--ids", "b35", "--gate"])

    assert code == 1
    assert "[FAIL]" in capsys.readouterr().out


def test_eval_unknown_ids_are_a_usage_error(offline_env, capsys):
    assert main(["eval", "--ids", "b01,zz9"]) == 2
    assert "zz9" in capsys.readouterr().err


def test_eval_llm_mode_requires_model(offline_env, capsys):
    assert main(["eval", "--planner", "auto"]) == 2


def test_catalog_command_validates_semantic_layer(offline_env, capsys):
    code = main(["catalog"])
    out = capsys.readouterr().out

    assert code == 0
    assert "融资事件" in out
    assert "[OK]" in out


def test_catalog_json(offline_env, capsys):
    assert main(["catalog", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["dataset"] == "znjz"


def test_build_demo_is_idempotent(tmp_path, capsys):
    target = tmp_path / "demo.duckdb"

    assert main(["build-demo", "--path", str(target)]) == 0
    assert target.exists()
    assert "企业基本信息" in capsys.readouterr().out
    assert main(["build-demo", "--path", str(target)]) == 0
    assert "已是最新" in capsys.readouterr().out


def test_doctor_reports_configuration_without_secrets(offline_env, monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret-value")

    code = main(["doctor"])
    out = capsys.readouterr().out

    assert code == 0
    assert "sk-test-secret-value" not in out
    assert "duckdb-demo" in out


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["mcp"], ("stdio", {})),
        (
            ["mcp", "--transport", "streamable-http", "--port", "9001"],
            ("streamable-http", {"host": "127.0.0.1", "port": 9001}),
        ),
    ],
)
def test_mcp_command_runs_server_with_chosen_transport(monkeypatch, argv, expected):
    calls = []

    class FakeServer:
        def run(self, transport, **kwargs):
            calls.append((transport, kwargs))

    monkeypatch.setattr("text2sql.mcp_server.build_server", lambda **_: FakeServer())

    assert main(argv) == 0
    assert calls == [expected]


def test_serve_starts_uvicorn_with_app_factory(offline_env, monkeypatch):
    calls = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: calls.update(app=app, **kwargs))

    assert main(["serve", "--port", "8123"]) == 0
    assert calls["app"] == "text2sql.api.app:create_app"
    assert calls["factory"] is True
    assert calls["port"] == 8123
