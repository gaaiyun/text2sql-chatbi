"""Streamlit 渲染组件与运行时资源。

- 配置每次运行都重新读取（便宜，且访问口令等变更立即生效）；
- 智能体按“规划方式 + 配置指纹”缓存为进程级资源：演示库生成和值索引扫描只做一次，
  各浏览器会话共享同一个智能体，靠各自的 thread_id 隔离对话记忆。
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import streamlit as st

from text2sql import __version__
from text2sql.config import Settings
from text2sql.ui.views import (
    NODE_LABELS,
    STATUS_BADGES,
    chart_for,
    csv_bytes,
    detail_text,
    kpis_for,
    result_frame,
    steps_frame,
    trace_frame,
)

REPO_URL = "https://github.com/gaaiyun/text2sql-chatbi"
PLANNER_MODES = {"auto": "自动", "semantic": "语义层", "llm": "SQL 智能体"}
PLANNERS = {"auto": ("semantic", "llm"), "semantic": ("semantic",), "llm": ("llm",)}
PLANNER_LABELS = {"semantic": "语义层编译", "llm": "SQL 智能体"}


# --------------------------------------------------------------------------- 运行时


def _secrets() -> dict[str, Any]:
    try:
        return {k: v for k, v in st.secrets.items() if not isinstance(v, dict)}
    except Exception:  # noqa: BLE001 - 本地没有 secrets.toml 时 Streamlit 会抛异常
        return {}


def load_settings() -> Settings:
    return Settings.from_env(extra=_secrets())


def _fingerprint(settings: Settings) -> str:
    secret_parts = [
        settings.llm.api_key if settings.llm else "",
        settings.mysql.password if settings.mysql else "",
    ]
    return hashlib.sha256((repr(settings) + "|".join(secret_parts)).encode("utf-8")).hexdigest()


@st.cache_resource(show_spinner="正在准备数据库连接与值索引……")
def _cached_agent(planner: str, fingerprint: str, _settings: Settings) -> Any:
    from text2sql.agent.graph import Text2SQLAgent

    return Text2SQLAgent.from_settings(_settings, planners=PLANNERS[planner])


def get_agent(settings: Settings, planner: str = "auto") -> Any:
    if planner == "llm" and settings.llm is None:
        planner = "semantic"
    return _cached_agent(planner, _fingerprint(settings), settings)


def available_modes(settings: Settings) -> dict[str, str]:
    if settings.llm is None:
        return {"auto": PLANNER_MODES["auto"], "semantic": PLANNER_MODES["semantic"]}
    return dict(PLANNER_MODES)


def password_gate(settings: Settings) -> bool:
    """配置了 APP_PASSWORD 时要求输入口令；返回是否放行。"""
    if not settings.app_password or st.session_state.get("authenticated"):
        return True
    st.title("Text2SQL 数据分析智能体")
    with st.form("login"):
        password = st.text_input("访问口令", type="password")
        submitted = st.form_submit_button("进入", type="primary")
    if submitted:
        if hmac.compare_digest(password.encode("utf-8"), settings.app_password.encode("utf-8")):
            st.session_state["authenticated"] = True
            st.rerun()
        st.error("口令不正确")
    return False


def sidebar_runtime(settings: Settings) -> None:
    with st.sidebar:
        st.markdown("#### 运行环境")
        if settings.database == "demo":
            st.markdown("数据源：**合成演示库**（DuckDB）")
            st.caption("按生产 DDL 生成的 5000 家合成企业，数字仅用于演示")
        else:
            st.markdown("数据源：**MySQL**（只读会话）")
        if settings.llm:
            st.markdown(f"模型：**{settings.llm.model}**")
        else:
            st.markdown("模型：**未配置**")
            st.caption("仅启用语义层路径；配置 OPENAI_API_KEY 后启用 SQL 智能体")
        st.caption(f"v{__version__} · [源码与文档]({REPO_URL})")


# --------------------------------------------------------------------------- 结果渲染


def _header(result: dict[str, Any]) -> None:
    label, color = STATUS_BADGES.get(result["status"], (result["status"], "gray"))
    with st.container(horizontal=True, gap="small", vertical_alignment="center"):
        st.badge(label, color=color)
        if result.get("planner"):
            st.badge(PLANNER_LABELS.get(result["planner"], result["planner"]), color="violet")
        st.caption(f"{result.get('elapsed_ms', 0):.0f} ms · {result.get('backend', '')}")
    if result.get("effective_question") and result["effective_question"] != result["question"]:
        st.caption(f"已理解为：{result['effective_question']}")


def _result_tab(result: dict[str, Any], key: str) -> None:
    columns, rows = result["columns"], result["rows"]
    kpis = kpis_for(result.get("chart"), rows)
    if kpis:
        with st.container(horizontal=True):
            for name, value in kpis:
                st.metric(name, value, border=True)
    chart = chart_for(result.get("chart"), columns, rows)
    if chart is not None:
        st.altair_chart(chart, width="stretch")
        if result["chart"].get("reason"):
            st.caption(f"图表：{result['chart']['reason']}")
    st.dataframe(result_frame(columns, rows), hide_index=True, width="stretch")
    note = f"共 {result['row_count']} 行"
    if result.get("truncated"):
        note += "，已达到行数上限，结果被截断"
    st.caption(note)
    with st.container(horizontal=True):
        st.download_button(
            "下载 CSV",
            csv_bytes(columns, rows),
            file_name="result.csv",
            mime="text/csv",
            key=f"{key}-csv",
            icon=":material/download:",
        )
        st.download_button(
            "下载分析报告",
            result.get("report", ""),
            file_name="report.md",
            mime="text/markdown",
            key=f"{key}-report",
            icon=":material/description:",
        )


def _sql_tab(result: dict[str, Any]) -> None:
    st.code(result.get("safe_sql") or result.get("sql") or "", language="sql", wrap_lines=True)
    guard = result.get("guard") or {}
    for change in guard.get("modifications") or []:
        st.caption(f"安全门改写：{change}")
    for warning in guard.get("warnings") or []:
        st.caption(f"安全门提示：{warning}")
    executed = result.get("executed_sql")
    if executed and executed != result.get("safe_sql"):
        with st.expander("演示库实际执行的 DuckDB 方言 SQL"):
            st.code(executed, language="sql", wrap_lines=True)


def _scope_tab(result: dict[str, Any]) -> None:
    if result.get("description"):
        st.markdown(f"**统计口径**：{result['description']}")
    for note in result.get("assumptions") or []:
        st.markdown(f"- {note}")
    source = result.get("narrative_source")
    if source == "llm":
        st.caption("解读由模型生成，其中的数字已与查询结果逐一核对")
    else:
        st.caption("解读由查询结果直接生成，不经过模型")


def _trace_tab(result: dict[str, Any]) -> None:
    frame = trace_frame(result.get("trace") or [])
    longest = float(frame["耗时（ms）"].max()) if not frame.empty else 1.0
    st.dataframe(
        frame,
        hide_index=True,
        width="stretch",
        column_config={
            "耗时（ms）": st.column_config.ProgressColumn(
                "耗时（ms）", min_value=0.0, max_value=max(longest, 1.0), format="%.1f"
            )
        },
    )
    usage = result.get("usage") or {}
    if usage.get("calls"):
        st.caption(
            f"模型调用 {usage['calls']} 次，输入 {usage.get('prompt_tokens', 0)} tokens，"
            f"输出 {usage.get('completion_tokens', 0)} tokens"
        )


def _quality_tab(result: dict[str, Any]) -> None:
    quality = result.get("quality") or {}
    icons = {
        "pass": ":material/check_circle:",
        "warn": ":material/warning:",
        "fail": ":material/cancel:",
    }
    st.metric("质量得分", f"{quality.get('score', 0):.2f}")
    for check in quality.get("checks") or []:
        st.markdown(f"{icons.get(check['status'], '')} {check['detail']}")


def _steps_tab(result: dict[str, Any]) -> None:
    st.dataframe(steps_frame(result["agent_steps"]), hide_index=True, width="stretch")
    with st.expander("工具调用参数"):
        st.json([{"tool": s["tool"], "arguments": s["arguments"]} for s in result["agent_steps"]])


def render_result(result: dict[str, Any], *, key: str, on_suggestion: Any = None) -> None:
    _header(result)
    if result["status"] != "answered":
        (st.error if result["status"] in ("rejected", "failed") else st.warning)(
            result.get("message") or result.get("error") or "未能回答"
        )
        suggestions = result.get("suggestions") or []
        if suggestions and on_suggestion is not None:
            st.caption("可以试试这些问题：")
            with st.container(horizontal=True):
                for index, suggestion in enumerate(suggestions):
                    st.button(
                        suggestion,
                        key=f"{key}-suggest-{index}",
                        on_click=on_suggestion,
                        args=(suggestion,),
                    )
        if result.get("agent_steps"):
            with st.expander("SQL 智能体的尝试过程"):
                _steps_tab(result)
        return

    st.markdown(result.get("answer") or "")
    names = ["结果", "SQL", "口径", "执行轨迹", "质量检查"]
    if result.get("agent_steps"):
        names.append("智能体步骤")
    tabs = st.tabs(names)
    with tabs[0]:
        _result_tab(result, key)
    with tabs[1]:
        _sql_tab(result)
    with tabs[2]:
        _scope_tab(result)
    with tabs[3]:
        _trace_tab(result)
    with tabs[4]:
        _quality_tab(result)
    if result.get("agent_steps"):
        with tabs[5]:
            _steps_tab(result)


def progress_line(event: dict[str, Any]) -> str:
    if event.get("type") == "agent_step":
        mark = "成功" if event.get("ok") else "失败"
        return f"工具 `{event.get('tool')}` {mark}：{event.get('summary', '')}"
    label = NODE_LABELS.get(event.get("node", ""), event.get("node", ""))
    detail = detail_text(event.get("detail"))
    return f"**{label}** · {float(event.get('ms') or 0):.1f} ms" + (
        f" · {detail}" if detail else ""
    )
