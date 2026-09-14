"""工作原理：工作流图由编译后的 LangGraph 直接导出，与代码中的路由一致。"""

import streamlit as st

from text2sql.ui.components import REPO_URL, get_agent, load_settings
from text2sql.ui.views import graph_edges, workflow_dot

settings = load_settings()
agent = get_agent(settings, "auto")

st.title("工作原理")
st.markdown(
    "一次提问是一次 LangGraph 状态机运行。实线是固定流转，虚线是条件路由："
    "安全门拒绝、执行报错或结果可疑时进入“诊断修复”，把具体原因和真实取值反馈给 SQL 智能体重写，"
    "最多重试两次；语义层给出的 SQL 是确定的，不会进入修复循环。"
)
st.graphviz_chart(workflow_dot(graph_edges(agent.graph)), width="stretch")

left, right = st.columns(2)
with left:
    st.markdown(
        """
##### 双路径规划
- **语义层编译**：规则解析器把问题映射到 YAML 里定义的实体、指标、维度和筛选，编译成确定的 SQL。
  只有问题里每个实义词都能被解释时才出手，零模型调用，毫秒级返回。
- **SQL 智能体**：语义层放弃的长尾问题交给带工具的模型。它先检索表、查看字段真实取值、
  试运行 SQL，确认命中数据后再提交；工具结果和最终执行走同一道安全门。

##### 多轮对话
同一会话的历史存在 LangGraph 检查点里。“那深圳呢”“按年份看呢”会按语义组件
替换上一轮问题再解析，改写结果显示给用户确认。
"""
    )
with right:
    st.markdown(
        """
##### 安全门（sqlglot 语法树）
单条语句、只允许 SELECT；表白名单；禁止跨库和系统函数；禁止 `SELECT *`；
个人信息字段不可读；逐列校验字段存在并给出相近字段提示；自动补齐或收紧 LIMIT；
执行前用 EXPLAIN 预估代价；MySQL 会话本身也是只读的。

##### 结果质量
执行成功不等于答对。结果为空时检查条件取值是否真实存在；解读里的数字必须能在结果中找到，
否则退回确定性解读；另有截断、排名数量、时间顺序、宽表占位行等 8 项检查。
"""
    )

st.caption(
    f"设计取舍与评测方法见仓库文档：[DESIGN.md]({REPO_URL}/blob/main/docs/DESIGN.md) · "
    f"[EVALUATION.md]({REPO_URL}/blob/main/docs/EVALUATION.md)"
)
