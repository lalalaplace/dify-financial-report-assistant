# Dify 工作流设计

## 核心能力

- 将自然语言问题解析为结构化查询意图。
- 自动识别公司、股票代码、年份、报告期和财务指标。
- 生成 PostgreSQL `SELECT` 查询。
- 调用 FastAPI 查询接口获取数据库结果。
- 按需调用 FastAPI 图表接口生成 PNG 图表。
- 输出带数据来源说明的中文回答。

## 工作流文件

`dify/workflow_export.yml` 为公开展示用脱敏模板。它不包含真实模型供应商配置、账号密码、API Key 或私有网络地址；导入后需要按本地 Dify 环境补全节点配置。

## SQL 安全策略

- 只允许 `SELECT`。
- 禁止 `INSERT`、`UPDATE`、`DELETE`、`DROP`、`ALTER`、`TRUNCATE`、`CREATE` 等关键字。
- 只允许访问白名单财务表。
- 对模糊问题先追问，不直接猜测。

## 脱敏原则

工作流导出文件不得包含 Dify 登录账号、密码、真实 API Key、数据库密码和个人本地路径。
