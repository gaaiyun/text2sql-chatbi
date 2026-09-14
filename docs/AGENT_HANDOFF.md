# 交接文档

> 唯一一份交接文档。影响开发、部署、数据或验收的变化直接更新这里。
> 数字分两类：**结构性**（测试文件数、节点数、数据集数，改代码才会变）与**易变**（测试总数、评测指标、构建大小，每次运行可能变），后者以最近一次实际运行为准并写明日期。

## 1. 现状（2026-09-14）

- 仓库：`gaaiyun/text2sql-chatbi`（公开，v2 独立发布，干净历史）。本地工作目录 `G:\text2sql-analysis`，远端名 `chatbi`。
- 旧仓库 `gaaiyun/text2sql-analysis` 保留 v1 与完整开发历史，未改动；其历史提交里有生产数据库地址，**数据库应只允许白名单 IP 访问**。
- 在线站点：见 README 顶部链接（Cloudflare Workers，模型走账号的 Workers AI 免费额度）。
- 本地 Python 环境：`G:\text2sql-analysis\.venv`（Python 3.13）；站点构建缓存 `G:\dev-cache\text2sql-site`。

## 2. 先读什么

1. [README.md](../README.md)：能力、评测、快速开始
2. [ARCHITECTURE.md](ARCHITECTURE.md)：分层、工作流、SQL 智能体、浏览器引擎、数据集
3. [DESIGN.md](DESIGN.md)：为什么这样设计
4. [DEPLOYMENT.md](DEPLOYMENT.md)：四种运行方式与全部配置项
5. [EVALUATION.md](EVALUATION.md)：评测方法与最近结果

## 3. 验证命令

```powershell
.\.venv\Scripts\python.exe -m ruff format --check text2sql tests scripts
.\.venv\Scripts\python.exe -m ruff check text2sql tests scripts
.\.venv\Scripts\python.exe -m pytest                       # 易变：774 项（2026-09-14）
.\.venv\Scripts\python.exe -m text2sql eval --gate --quiet  # 离线语义层门禁
.\.venv\Scripts\python.exe scripts\check_security.py
.\.venv\Scripts\python.exe scripts\check_deploy_readiness.py
```

站点：`$env:T2S_SITE_CACHE="G:\dev-cache\text2sql-site"; python scripts\build_site.py`，然后 `cd site; npx wrangler@4 deploy`。
部署后 Cloudflare 边缘可能有约一分钟旧页面，验证时带查询参数刷新。

## 4. 结构性事实

- 工作流 11 个节点；两种编排器（LangGraph / 顺序执行器）共用路由表。
- SQL 智能体 5 个只读工具 + `submit_sql`；每轮最多 8 次模型调用。
- 数据集：企业库（合成、手写语义层、62 题评测集）、茶饮门店示例、Northwind、Chinook、Gapminder、OWID 碳排放、上传文件。
- 开源原始文件固定在 `scripts/opendata.py` 的 `SOURCES`（提交 SHA + sha256）；数据卡片在 `text2sql/datasets/cards/`。
- Worker 模型：`@cf/zai-org/glm-4.7-flash`，备用 `@cf/qwen/qwen3-30b-a3b-fp8`；限流每 IP 每分钟 30 次。

## 5. 约定

- 提交信息：中文，`类型(范围): 描述`，类型限 feat / fix / refactor / docs / style / test / chore，不加任何 AI 署名。
- 不提交真实数据；企业库是按生产表结构生成的合成数据。
- 个人信息字段（`oper_name` 等、上传文件里疑似个人信息的列）在意图预检与安全门两处拦截，不要放宽。
- 新增配置项时同步 `.env.example`、`docs/DEPLOYMENT.md` 与 `scripts/check_deploy_readiness.py` 的 `CONFIG_KEYS`（有测试核对）。
- 公开本地数据集前先确认许可证（CSMAR、Wind 等商业库不能放进站点）。

## 6. 已知问题与后续

- 评测集与语义层出自同一作者；长尾题的 SQL 智能体结果受模型波动影响，数字以 EVALUATION.md 最近一次运行为准。
- Workers AI 免费额度每天 10,000 neurons，一道长尾题约 1 万到 2.5 万 token；高峰期可能用尽。
- workers.dev 子域名在国内访问不稳定，正式展示建议绑定自有域名。
- 浏览器端没有查询超时中断（WebAssembly 无线程），依赖代价估算。
- Docker 镜像未在本机构建验证（避免占用 C 盘），`docker compose config` 已通过。

## 7. 开发史

| 时间 | 阶段 | 要点 |
|---|---|---|
| 2026-07 | v1 | `AgentRuntime` 线性流程 + Streamlit + n8n；sqlparse 安全层；只看“是否返回了行”的验收 |
| 2026-08 | v1 维护 | 统一模型网关；Streamlit Cloud 部署文档 |
| 2026-09 上旬 | v2 重构 | 语义层 + SQL 智能体双路径；sqlglot 列级安全门；LangGraph 工作流与检查点；FastAPI SSE；MCP server；执行准确率评测集；Streamlit 多页界面 |
| 2026-09-13 | 浏览器站点 | Pyodide + DuckDB WASM 跑同一份代码；顺序执行器；Cloudflare Workers 静态资源 + Workers AI |
| 2026-09-14 | ChatBI | 工业控制台视觉；上传文件自动语义层；四个开源数据集与数据卡片；图表切换、SQL 修改重跑、看板、存为示例；试运行逐列概况、步数兜底、个人信息预检、计数形态修复；Workers AI 双路径评测 |

会话记录：Claude Code 会话 `939d0b29-41a7-453f-8c38-263763b95e4c`（`C:\Users\gaaiy\.claude\projects\G--ClaudeCode\`）。
