# 安全说明

## 威胁与对策

| 风险 | 对策 | 位置 |
|---|---|---|
| 模型或用户写出修改数据的 SQL | 意图预检拒绝写操作；安全门只允许单条 SELECT / WITH / UNION，树中任意位置出现写操作即拒绝；MySQL 会话设为只读 | `agent/intent.py`、`sql/guard.py`、`db/backends.py` |
| 提示词注入 | 确定性规则在任何模型调用之前拦截；模型只能调用固定的只读工具 | `agent/intent.py`、`agent/tools.py` |
| 查询个人信息 | 意图预检拒绝涉及个人信息的问题；安全门拦截敏感列；上传文件按列名识别疑似个人信息，不抽样取值、不可查询 | `agent/intent.py`、`sql/guard.py`、`datasets/upload.py` |
| 跨库访问、系统表、危险函数 | 表白名单、禁止库名限定、函数白名单（`SLEEP`、`LOAD_FILE` 等） | `sql/guard.py` |
| 拖垮数据库 | 收紧 LIMIT；EXPLAIN 估算处理行数，超限拒绝；查询超时中断 | `sql/guard.py`、`agent/repair.py`、`db/backends.py` |
| 解读编造数字 | 解读默认由结果直接生成；启用模型解读时逐个核对数字出处，对不上就退回 | `agent/reflection.py` |
| 公开站点的模型接口被滥用 | Worker 只接受同源请求、按 IP 限流、限制请求体、工具名单与 max_tokens，模型在服务端固定，页面不持有密钥 | `site/worker/index.js` |
| 站点被注入脚本 | CSP：脚本只允许同源与 WebAssembly，禁止被嵌入 iframe | `site/src/_headers` |
| 密钥进入仓库或镜像 | `.gitignore` 与 `.dockerignore` 排除 `.env`、`secrets.toml`；提交前扫描公网 IP、Key、口令和 DEFINER | `scripts/check_security.py` |

## 已知边界

- 浏览器里的 DuckDB 运行在 WebAssembly 中，没有线程，查询超时中断不可用（代价估算仍然生效）。
- 意图预检是规则而不是模型判断：宁可误拒少量合法问题，也不让个人信息请求进入规划器。
- 上传的文件只在当前浏览器标签页里，不经过服务器；提问时表结构、字段取值样例和试运行的前几行会发给 Workers AI。

## 报告问题

发现安全问题请在 GitHub 私下提交 Security Advisory，不要在公开 Issue 里附带可利用的细节。
