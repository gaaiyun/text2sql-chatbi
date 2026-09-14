"""Streamlit 入口（Streamlit Cloud 的主文件）。

streamlit run streamlit_app.py
"""

import streamlit as st

from text2sql.ui.components import load_settings, password_gate, sidebar_runtime

st.set_page_config(
    page_title="Text2SQL 数据分析智能体",
    page_icon=":material/query_stats:",
    layout="wide",
)

settings = load_settings()
if not password_gate(settings):
    st.stop()

pages = st.navigation(
    [
        st.Page("app_pages/ask.py", title="问数", icon=":material/chat:", default=True),
        st.Page("app_pages/evaluation.py", title="评测", icon=":material/fact_check:"),
        st.Page("app_pages/semantic_layer.py", title="语义层", icon=":material/schema:"),
        st.Page("app_pages/how_it_works.py", title="工作原理", icon=":material/account_tree:"),
    ],
    position="top",
)
pages.run()
sidebar_runtime(settings)
