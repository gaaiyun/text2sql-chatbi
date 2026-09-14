"""问数：多轮对话，实时展示工作流进度，结果按结论 / SQL / 口径 / 轨迹 / 质量分栏。"""

import uuid

import streamlit as st

from text2sql.ui.components import (
    available_modes,
    get_agent,
    load_settings,
    progress_line,
    render_result,
)

AVATARS = {"user": ":material/person:", "assistant": ":material/query_stats:"}
settings = load_settings()
modes = available_modes(settings)
state = st.session_state
state.setdefault("thread_id", f"ui-{uuid.uuid4().hex[:12]}")
state.setdefault("messages", [])


def ask(question: str) -> None:
    state["pending"] = question


def new_conversation() -> None:
    state["thread_id"] = f"ui-{uuid.uuid4().hex[:12]}"
    state["messages"] = []


with st.sidebar:
    planner = st.segmented_control(
        "规划方式",
        options=list(modes),
        format_func=modes.get,
        default="auto",
        key="planner",
        help="自动：语义层优先，覆盖不到的长尾问题交给 SQL 智能体。"
        "语义层：只用规则编译，零模型调用。SQL 智能体：只用带工具的模型。",
    )
    st.button("新会话", icon=":material/add_comment:", on_click=new_conversation, width="stretch")
    st.caption("同一会话内可以追问，例如先问“广州市存续企业有多少家”，再问“那深圳呢”。")
    st.divider()

agent = get_agent(settings, planner or "auto")

st.title("智能制造企业数据问答")
st.caption(
    "用中文提问，智能体理解意图、生成并校验 SQL、执行查询，再给出只引用查询结果的结论。"
    "所有 SQL 都经过只读安全门。"
)

if not state["messages"]:
    examples = [e for e in agent.catalog.examples if not e.requires_llm or agent.sql_agent]
    st.markdown("##### 试试这些问题")
    with st.container(horizontal=True, gap="small"):
        for index, example in enumerate(examples):
            st.button(
                example.question,
                key=f"example-{index}",
                on_click=ask,
                args=(example.question,),
                help=example.category,
            )

for index, message in enumerate(state["messages"]):
    with st.chat_message(message["role"], avatar=AVATARS[message["role"]]):
        if message["role"] == "user":
            st.markdown(message["content"])
        else:
            render_result(message["result"], key=f"history-{index}", on_suggestion=ask)

question = st.chat_input("例如：近三年每年的融资事件数", max_chars=500) or state.pop(
    "pending", None
)
if question:
    state["messages"].append({"role": "user", "content": question})
    with st.chat_message("user", avatar=AVATARS["user"]):
        st.markdown(question)
    with st.chat_message("assistant", avatar=AVATARS["assistant"]):
        result = None
        with st.status("正在分析……", expanded=True) as status:
            for event in agent.stream(question, thread_id=state["thread_id"]):
                if event["type"] == "result":
                    result = event["result"]
                else:
                    status.markdown(progress_line(event))
            answered = result is not None and result["status"] == "answered"
            status.update(
                label=f"完成，用时 {result['elapsed_ms']:.0f} ms" if result else "执行失败",
                state="complete" if answered else "error",
                expanded=False,
            )
        if result is not None:
            state["messages"].append({"role": "assistant", "result": result})
            render_result(result, key=f"live-{len(state['messages'])}", on_suggestion=ask)
