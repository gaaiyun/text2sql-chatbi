"""评测：在页面上复现离线评测，指标与逐题结果都可以核对。"""

import pandas as pd
import streamlit as st

from text2sql.evaluation.runner import load_benchmark, run_evaluation
from text2sql.ui.components import get_agent, load_settings

settings = load_settings()
items = load_benchmark()
answerable = sum(1 for item in items if item.expect == "answer")

st.title("执行准确率评测")
st.markdown(
    f"评测集共 **{len(items)}** 题：{answerable} 道应答题附手写标准 SQL，"
    f"{len(items) - answerable} 道应拒题覆盖写操作、提示词注入、个人信息和领域外问题。"
    "判分方式是执行两边的 SQL、比较结果集（列顺序与列名无关，数值保留两位小数），"
    "不比较 SQL 文本。"
)
st.caption(
    "页面只运行语义层路径（零模型调用，约 2 秒）。SQL 智能体路径会产生模型费用，"
    "请在命令行运行：python -m text2sql eval --planner auto"
)

if st.button("运行离线评测", type="primary", icon=":material/play_arrow:"):
    agent = get_agent(settings, "semantic")
    progress = st.progress(0.0, text="准备中")
    done = []

    def on_item(outcome):
        done.append(outcome)
        progress.progress(len(done) / len(items), text=f"{outcome.id} {outcome.question}")

    summary, outcomes = run_evaluation(
        agent, items, backend=agent.backend, mode="semantic", on_item=on_item
    )
    progress.empty()
    st.session_state["evaluation"] = (summary, outcomes)

if "evaluation" in st.session_state:
    summary, outcomes = st.session_state["evaluation"]
    with st.container(horizontal=True):
        st.metric(
            "作答精确率", f"{summary.precision:.1%}", border=True, help="作答的题目中结果正确的比例"
        )
        st.metric("覆盖率", f"{summary.coverage:.1%}", border=True, help="应答题中作答的比例")
        st.metric(
            "执行准确率",
            f"{summary.execution_accuracy:.1%}",
            border=True,
            help="结果正确 / 应答题数",
        )
        st.metric("拒答准确率", f"{summary.refusal_accuracy:.1%}", border=True)
        st.metric("Schema 召回率", f"{summary.linking_recall:.1%}", border=True)
        st.metric(
            "延迟 P50 / P95",
            f"{summary.latency_p50_ms:.0f} / {summary.latency_p95_ms:.0f} ms",
            border=True,
        )

    st.markdown("##### 分类别结果")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "类别": category,
                    **{
                        "应答": row["answerable"],
                        "作答": row["answered"],
                        "正确": row["correct"],
                        "应拒": row["refusals"],
                        "正确拒答": row["refused_ok"],
                    },
                }
                for category, row in summary.by_category.items()
            ]
        ),
        hide_index=True,
        width="stretch",
    )

    st.markdown("##### 逐题结果")
    verdicts = {"全部": None, "答错": "wrong", "未作答": "declined", "拒答题": "refuse"}
    choice = st.segmented_control("筛选", list(verdicts), default="全部", key="eval-filter")

    def keep(outcome) -> bool:
        wanted = verdicts.get(choice or "全部")
        if wanted == "wrong":
            return outcome.correct is False or outcome.refused_ok is False
        if wanted == "declined":
            return outcome.expect == "answer" and outcome.correct is None
        if wanted == "refuse":
            return outcome.expect == "refuse"
        return True

    def verdict(outcome) -> str:
        if outcome.expect == "refuse":
            return "正确拒答" if outcome.refused_ok else "未拒答"
        return {True: "正确", False: "答错", None: "未作答"}[outcome.correct]

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "编号": o.id,
                    "类别": o.category,
                    "问题": o.question,
                    "判定": verdict(o),
                    "耗时（ms）": o.latency_ms,
                    "说明": o.reason,
                }
                for o in outcomes
                if keep(o)
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    st.caption(
        "未作答的题目是语义层主动拒答：宁可交给 SQL 智能体或说明做不到，也不给出看似合理的错误结果。"
    )
