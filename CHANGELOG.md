# 更新记录

## 2.0.0（2026-09）

本仓库是 v2 的独立发布；v1（`AgentRuntime` + Streamlit）在旧仓库 gaaiyun/text2sql-analysis 保留。

### 智能体
- 语义层编译与 SQL 智能体双路径，同一道 sqlglot 语法树安全门
- 11 个节点的工作流，路由表声明式定义；LangGraph（检查点保存会话）与顺序执行器可替换，测试逐题核对输出一致
- SQL 智能体用函数调用操作 5 个只读工具；试运行返回总行数与逐列概况；步数用尽时采用最后一次成功试运行的 SQL
- 修复回路：执行错误、可疑空结果（条件取值不存在）、计数问题按分组返回多行
- 意图预检在规划前拒绝写操作、提示词注入、个人信息请求和领域外问题
- 结果画像识别单值、类别对比、时间序列、多序列长表与宽表多指标；有据解读；8 项质量检查
- 页面确认过的问答可加入示例库，相似问题检索命中

### 数据
- 企业库（合成，按生产表结构生成）配手写语义层与 62 题评测集
- 上传 CSV / TSV / Excel / Parquet / JSON：导入 DuckDB，自动画像生成语义层
- 开源数据集：Northwind、Chinook（MIT）、Gapminder（CC0）、Our World in Data 碳排放（CC BY 4.0），
  原始文件固定到提交并校验 sha256，数据卡片补充中文字段名、同义词、表间关联与口径

### 入口
- 网页控制台：Python 与 DuckDB 以 WebAssembly 运行在浏览器，模型经 Cloudflare Worker 调用 Workers AI；
  数据集中心、执行账本、图表切换、SQL 修改重跑、看板
- CLI（ask / chat / eval / serve / mcp / doctor）、FastAPI（REST + SSE）、MCP server、Streamlit 多页界面
- Docker Compose 一键运行接口与界面

### 工程
- 774 项测试；离线评测门禁；敏感信息扫描与部署检查脚本；GitHub Actions
