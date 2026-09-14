# Text2SQL Agent 验收结果

## 最新后端验收

- 时间：2026-07-06 00:37:41
- Runtime：LangGraph
- 数据库：`znjz`
- LLM：OpenAI-compatible 中转站 OpenAI-compatible，模型 `grok-4.5`
- 本地产物目录：`output/agent_acceptance_20260706_003739/`

| 序号 | 问题 | 场景 | 状态 | 行数 |
|---|---|---|---|---:|
| 01 | 统计企业经营状态分布 | data_insight | success | 10 |
| 02 | 按行业统计企业数量 Top 10 | industry | success | 10 |
| 03 | 统计各融资轮次的企业数量和融资金额 | data_insight | success | 18 |
| 04 | 按年份统计招投标数量 | data_insight | success | 25 |
| 05 | 统计商标资质的申请年份分布 | data_insight | success | 20 |
| 06 | 统计企业地区分布 Top 20 | regional | success | 20 |
| 07 | 按成立年份统计企业数量趋势 | regional | success | 46 |
| 08 | 统计对外投资数量最多的企业 Top 10 | investment | success | 10 |
| 09 | 按注册资本区间统计企业数量 | data_insight | success | 6 |
| 10 | 查询一家企业的基本信息、融资、投资和招投标情况 | due_diligence | success | 1 |

每个问题均保存了：

- `*.json`：原始响应，包括 `sql`、`safe_sql`、`rows`、`analysis`、`safety`、`trace`
- `*.md`：Markdown 分析报告
- `index.md`：验收索引

## Streamlit UI 验收

- 时间：2026-07-06 00:34 左右
- URL：http://localhost:8502
- 本地产物目录：`output/streamlit_ui_20260706_0037/`
- Browser 插件：当前会话未提供 Browser skill，使用 Playwright fallback

| 检查项 | 结果 |
|---|---|
| 口令登录 | 通过 |
| 真实查询提交 | 通过 |
| Workflow backend | 页面显示 `langgraph` |
| 返回行数 | 10 |
| 报告渲染 | 通过，展示“核心发现”和“分析局限性” |
| 图表渲染 | 通过 |
| SQL/数据/Trace 标签 | 通过 |
| Console error | 0 |
| Console warning | 有 Vega/图表 warning，不影响结果展示 |

截图：

- `output/streamlit_ui_20260706_0037/01_login.png`
- `output/streamlit_ui_20260706_0037/02_query_form.png`
- `output/streamlit_ui_20260706_0037/03_result.png`

## 部署就绪验收

- PR：`https://github.com/gaaiyun/text2sql-analysis/pull/5`
- GitHub Actions：`test (3.11)`、`test (3.12)`、`lint` 均为 SUCCESS
- 本地完整测试：`181 passed, 13 skipped`
- Streamlit 烟测：`output/streamlit_smoke_20260706_0101/`，HTTP 200，进程停止后端口关闭
- 部署契约检查：`python scripts/check_streamlit_readiness.py` 全部 PASS

Streamlit Cloud 当前打开为登录页，当前环境没有可用账号登录态，无法代替账号所有者创建 Cloud App 或填写 Advanced settings secrets。公网发布需要在 Streamlit Cloud 控制台完成：

1. PR 合并到 `main` 后，选择 GitHub 仓库 `gaaiyun/text2sql-analysis`。
2. Branch 选择 `main`。
3. Main file path 填 `streamlit_app.py`，不要填 `/streamlit_app.py`。
4. 按 `docs/STREAMLIT_DEPLOY.md` 配置 secrets。
5. 轮换任何曾在聊天中出现过的模型 Key。
6. 确认 MySQL 允许 Streamlit Cloud 访问。

## 注意

- 验收使用真实 MySQL 和真实 LLM 调用。
- API Key 与数据库密码只通过进程环境变量传入，未写入仓库文件。
- 生产部署前应轮换任何曾在聊天中出现过的 Key。
