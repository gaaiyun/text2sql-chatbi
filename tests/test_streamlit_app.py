"""Streamlit 界面冒烟：AppTest 在进程内真实执行页面脚本（离线演示库，无模型）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

TIMEOUT = 120
APP = str(Path(__file__).resolve().parents[1] / "streamlit_app.py")


@pytest.fixture
def app(monkeypatch, demo_db_path):
    for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "LLM_PROVIDER", "DB_HOST_SCENARIO_1_3"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("APP_PASSWORD", "")
    monkeypatch.setenv("T2S_DATABASE", "demo")
    monkeypatch.setenv("T2S_DEMO_DB_PATH", str(demo_db_path))

    def factory() -> AppTest:
        at = AppTest.from_file(APP, default_timeout=TIMEOUT)
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        return at

    return factory


def markdown_text(at: AppTest) -> str:
    return "\n".join(str(m.value) for m in at.markdown)


def test_home_page_offers_example_questions(app):
    at = app()

    assert at.title[0].value == "智能制造企业数据问答"
    assert any(button.label == "广州市存续企业有多少家" for button in at.button)
    assert len(at.chat_input) == 1


def test_asking_renders_answer_with_result_tabs(app):
    at = app()

    at.chat_input[0].set_value("各行业门类的平均注册资本").run()

    assert not at.exception, [e.value for e in at.exception]
    assert [tab.label for tab in at.tabs][:5] == ["结果", "SQL", "口径", "执行轨迹", "质量检查"]
    assert "建筑业" in markdown_text(at)
    assert any("CASE SUBSTR" in code.value for code in at.code)


def test_follow_up_in_same_session_is_rewritten(app):
    at = app()

    at.chat_input[0].set_value("广州市存续企业有多少家").run()
    at.chat_input[0].set_value("那深圳呢").run()

    assert not at.exception, [e.value for e in at.exception]
    assert any("已理解为：深圳" in caption.value for caption in at.caption)


def test_example_button_asks_the_question(app):
    at = app()

    next(
        b for b in at.button if b.label == "资质状态分布" or b.label == "统计企业经营状态分布"
    ).click().run()

    assert not at.exception, [e.value for e in at.exception]
    assert at.tabs


def test_declined_question_shows_reason_and_suggestions(app):
    at = app()

    at.chat_input[0].set_value("每个城市招投标记录最多的企业分别是哪家").run()

    assert at.warning and "窗口函数" in at.warning[0].value
    assert any(b.key and "suggest" in b.key for b in at.button)


def test_password_gate_blocks_everything_else(app, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "s3cret")
    at = AppTest.from_file(APP, default_timeout=TIMEOUT)
    at.run()

    assert len(at.text_input) == 1
    assert len(at.chat_input) == 0

    at.text_input[0].set_value("s3cret")
    at.button[0].click().run()

    assert len(at.chat_input) == 1


def test_evaluation_page_reproduces_metrics(app):
    at = app()
    at.switch_page("app_pages/evaluation.py").run()

    next(b for b in at.button if b.label == "运行离线评测").click().run()

    assert not at.exception, [e.value for e in at.exception]
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["作答精确率"] == "100.0%"
    assert metrics["拒答准确率"] == "100.0%"


def test_semantic_layer_debugger_compiles_sql(app):
    at = app()
    at.switch_page("app_pages/semantic_layer.py").run()

    assert not at.exception, [e.value for e in at.exception]
    assert any("fx.`round_date`" in code.value for code in at.code)


def test_how_it_works_page_renders(app):
    at = app()
    at.switch_page("app_pages/how_it_works.py").run()

    assert not at.exception, [e.value for e in at.exception]
    assert at.title[0].value == "工作原理"
