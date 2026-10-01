# 大坝巡检、缺陷与应急管理

安排巡检，记录渗流、位移、裂缝等缺陷并跟踪修复、复检和应急预案。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8316
```

默认端口为`8316`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/items/{id}/readings/backfill`，断网后按批次补传渗流/位移读数
- `GET /api/items/{id}/readings`
- `GET /api/items/{id}/reviews`
- `POST /api/items/{id}/reviews/recalculate`
- `GET /api/audit`

允许角色：inspector, dam_engineer, emergency_manager, viewer。异常值比控制阈值越高，缺陷优先级越高；应急处置缺陷必须完成复检并记录证据后才能关闭。

## 断网补传读数

巡检员在山里断网时先记录渗流（seepage）和位移（displacement）读数，回网后调用补传接口并入已有缺陷。

- 读数携带现场观测时间 `observed_at`，即使早于办公室处置时间也会保留并参与复核重算，不会被丢弃。
- 补传采用乐观并发：请求体中的 `expected_version` 与缺陷当前版本不一致时返回 `409`，响应体带 `current_version`，后到者据此刷新重试。
- 补传为整批事务：批次登记、读数入库、旧复核失效、新复核重算与审计追加要么全部成功要么全部回滚，不留半条记录。
- 批次幂等：`batch_id` 全局唯一，失败后按原批次重试不会重复写入；未传 `batch_id` 时服务端生成并在响应中返回。
- 读数更新后旧复核结论标记 `stale=1` 失效，新结论按最新读数与阈值重算生成；原处置记录、审计链与复核历史仍可查询。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
