"""语义层：指标口径一览，以及“问题 → 计划 → SQL”的解析调试台。"""

import pandas as pd
import streamlit as st

from text2sql.semantic.explain import explain_question
from text2sql.ui.components import get_agent, load_settings

settings = load_settings()
agent = get_agent(settings, "semantic")
catalog = agent.catalog
summary = catalog.summary()

st.title("语义层")
st.markdown(
    "语义层把业务口径写成配置：什么算“存续企业”、融资金额怎么补齐、哪些表有占位行。"
    "规则解析器只在问题里每个实义词都能被语义层解释时才编译 SQL，否则放弃并交给 SQL 智能体。"
)
with st.container(horizontal=True):
    for label, key in (
        ("实体", "entities"),
        ("指标", "metrics"),
        ("维度", "dimensions"),
        ("筛选", "filters"),
    ):
        st.metric(label, len(summary[key]), border=True)
    st.metric("业务规则", len(summary["rules"]), border=True)


def frame(section: str, extra: dict[str, str] | None = None) -> pd.DataFrame:
    rows = []
    for item in summary[section]:
        row = {
            "键": item["key"],
            "名称": item["label"],
            "同义词": "、".join(item.get("synonyms", [])),
        }
        for field, title in (extra or {}).items():
            value = item.get(field)
            row[title] = "、".join(value) if isinstance(value, list) else value
        rows.append(row)
    return pd.DataFrame(rows)


debugger, catalog_tab, rules_tab, tables_tab = st.tabs(
    ["解析调试台", "指标与维度", "业务规则", "表与字段"]
)

with debugger:
    question = st.text_input(
        "输入一个问题，查看语义层如何理解它", value="2020年以来有融资记录的企业数量"
    )
    if question:
        explained = explain_question(
            question,
            parser=agent.parser,
            catalog=catalog,
            guard=agent.guard,
            max_rows=settings.max_rows,
        )
        matches = [
            {"片段": m["surface"], "类型": m["kind_label"], "含义": m["meaning"]}
            for m in explained["matches"]
            if m["kind"] != "stop"
        ]
        left, right = st.columns([2, 3])
        with left:
            st.markdown("**词语匹配**")
            st.dataframe(pd.DataFrame(matches), hide_index=True, width="stretch")
            if explained["unexplained"]:
                st.warning("无法解释：" + "、".join(explained["unexplained"]))
        with right:
            if explained["sql"] is None:
                st.warning(f"语义层放弃：{explained['declined']}")
                st.caption("放弃不是失败：这类问题会交给 SQL 智能体，或者明确告诉用户做不到。")
            else:
                st.markdown(f"**计划**：{explained['description']}")
                for note in explained["assumptions"]:
                    st.caption(note)
                st.code(explained["sql"], language="sql", wrap_lines=True)
                with st.expander("查询计划 JSON"):
                    st.json(explained["plan"])

with catalog_tab:
    st.markdown("##### 实体")
    st.dataframe(frame("entities", {"table": "表"}), hide_index=True, width="stretch")
    st.markdown("##### 指标")
    st.dataframe(frame("metrics", {"entities": "适用实体"}), hide_index=True, width="stretch")
    st.markdown("##### 维度")
    st.dataframe(frame("dimensions"), hide_index=True, width="stretch")
    st.markdown("##### 筛选条件")
    st.dataframe(frame("filters", {"group": "互斥组"}), hide_index=True, width="stretch")

with rules_tab:
    st.caption("这些规则同时写进 SQL 智能体的系统提示词，也是语义层编译时遵守的口径。")
    for index, rule in enumerate(summary["rules"], start=1):
        st.markdown(f"{index}. {rule}")

with tables_tab:
    names = list(catalog.tables)
    chosen = st.selectbox(
        "表或视图", names, format_func=lambda n: f"{n}（{catalog.tables[n].label}）"
    )
    doc = catalog.tables[chosen]
    st.markdown(f"**{doc.label}**：{doc.description}")
    if doc.grain:
        st.caption(f"粒度：{doc.grain}")
    physical = agent.schema.get(chosen)
    rows = []
    for column in physical.columns:
        if catalog.is_sensitive(chosen, column.name):
            continue
        column_doc = doc.columns.get(column.name)
        rows.append(
            {
                "字段": column.name,
                "类型": column.raw_type,
                "含义": column_doc.label if column_doc else "",
                "说明": (column_doc.description or column_doc.value_hint or "")
                if column_doc
                else "",
            }
        )
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption("法定代表人等个人信息字段不在此展示，也无法被任何查询读取。")
