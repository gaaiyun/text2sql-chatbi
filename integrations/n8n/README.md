# n8n 工作流集成指南（统一 Agent Runtime）

本文档说明如何将 Text2SQL 与 n8n 集成：导入工作流、配置节点与凭证、测试方法。

## 前置条件

1. **API 服务**运行在 `http://localhost:8000`
   - 启动：`python -m text2sql serve --host 0.0.0.0 --port 8000`（或 `docker compose up api`）
2. 按 `.env.example` 配置模型与数据库（不配置时使用合成演示库，只启用语义层路径）
3. n8n 已安装并可访问

## 工作流导入步骤

1. 打开 n8n 界面，进入 **Workflows**
2. 点击 **Import from File** 或 **Import from URL**
3. 选择 **`integrations/n8n/text2sql_workflow.json`**
4. 导入后工作流会包含以下节点（按执行顺序）：
   - **Webhook Trigger**：接收 POST 请求
   - **Task Classifier**：根据问题关键词识别场景（数据洞察/地区产业/行业分析/招商清单/尽调报告）
   - **Agent SQL Generator**：调用统一 Agent 服务 `POST /api/agent/query`
   - **Data Processor**：解析 SQL、数据结果和 Agent 分析
   - **LLM Analysis**：调用大模型做深度分析（需配置凭证）
   - **Markdown Formatter**：组装 Markdown 报告
   - **Response Webhook**：返回最终结果

## 节点配置说明

### Webhook Trigger

- **Path**: `text2sql`（可改）
- **Method**: POST
- 请求体示例：`{ "query": "分析近三年融资趋势" }` 或 `{ "body": { "query": "..." } }`
- 下游通过 `$json.query` 或 `$json.body.query` 获取用户问题

### Task Classifier（Code 节点）

- 从输入中取 `query` 或 `body.query` 作为 `originalQuery`
- 根据关键词匹配场景，输出 `originalQuery`、`scenario`（中文场景名）、`timestamp`

### Agent SQL Generator（HTTP Request）

- **URL**: `http://localhost:8000/api/agent/query`
- **Method**: POST
- **Content-Type**: application/json
- **Body**:
  - `question`: 用户自然语言问题（来自 Task Classifier 的 `originalQuery`）
  - `scenario`: 场景标识（data_insight / regional / industry / investment / due_diligence）
  - `password`: 可选；如果 API 服务配置了 `APP_PASSWORD`，需要传入访问口令
- 返回字段：`sql`、`safe_sql`、`rows`、`columns`、`analysis`、`report`、`chart`、`trace`、`error`（可选）

### LLM Analysis

- 需配置 **OpenAI 兼容 API** 凭证：在 n8n 中创建凭证，类型选 “OpenAI API”，填写 Base URL 与 API Key
- 节点中 `credentials.openAiApi` 指向该凭证的 ID

### Response Webhook

- 使用 **Respond to Webhook** 将 Markdown 或 JSON 返回给调用方
- 当前为 JSON 响应，包含 `output`（Markdown 文本）、`format`、`scenario`

## 凭证设置

1. **Agent 服务**：如果配置了 `APP_PASSWORD`，在 HTTP Request body 或 `X-App-Password` header 中传递
2. **LLM（可选）**：
   - 类型：OpenAI API 兼容
   - Base URL 与 API Key：从所用的模型服务获取
   - 在 “LLM Analysis” 节点中选择该凭证

## 测试方法

### 1. 健康检查

```bash
curl http://localhost:8000/health
```

应返回包含 `agent` 状态的 JSON。v2 的新接口是 `POST /api/v1/query`（完整结果）与
`POST /api/v1/query/stream`（SSE 逐节点推送）；这里用的 `/api/agent/query` 保持 v1 的请求与返回格式。

### 2. 直接调用 Agent 查询

```bash
curl -X POST http://localhost:8000/api/agent/query \
  -H "Content-Type: application/json" \
  -d "{\"question\":\"分析近三年融资趋势\",\"scenario\":\"data_insight\"}"
```

应返回包含 `safe_sql`、`rows`、`analysis` 的 JSON。

### 3. 在 n8n 中测试工作流

- 保存工作流后，打开 **Webhook Trigger** 节点，复制 “Test URL”
- 使用 Postman 或 curl 向该 URL 发送 POST：
  - Body (JSON): `{ "query": "分析近三年融资趋势" }`
- 检查执行结果：Data Processor 应得到 `sql`，LLM Analysis 输出分析内容，Response 返回完整报告

### 4. 场景与数据库对应关系

| 场景     | scenario 值      | 数据库 profile |
|----------|------------------|----------------|
| 数据洞察 | data_insight     | znjz           |
| 地区产业 | regional         | znjz           |
| 行业分析 | industry         | znjz           |
| 招商清单 | investment       | znjz           |
| 尽调报告 | due_diligence    | znjz           |

第一版统一使用 `znjz` 智能制造数据库，旧库只保留兼容入口。

## 故障排查

- **503 Agent Runtime 未就绪**：检查 `OPENAI_API_KEY`、`DB_HOST_SCENARIO_1_3`、`DB_NAME_SCENARIO_1_3`
- **401 访问口令错误**：检查 n8n body 或 header 中的口令是否等于 `APP_PASSWORD`
- **SQL 被拒绝**：查看返回的 `safety.errors`，通常是非 SELECT、多语句或引用了非白名单表
- **n8n 调用超时**：确认 n8n 与 API 服务在同一网络或将 URL 改为可访问的 host
