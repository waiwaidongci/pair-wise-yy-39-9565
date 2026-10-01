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
- `POST /api/items/{id}/readings/sync`，离线补传渗流/位移读数（见下）
- `GET /api/items/{id}/readings`，按观测时间排序的读数时间线
- `GET /api/items/{id}/conclusions`，复核结论历史（含已失效）
- `GET /api/audit`，支持`?item_id=`过滤

允许角色：inspector, dam_engineer, emergency_manager, viewer。异常值比控制阈值越高，缺陷优先级越高；应急处置缺陷必须完成复检并记录证据后才能关闭。

## 离线补传

巡检员断网时记录读数，回网后按批次补传：

```json
POST /api/items/{id}/readings/sync
{
  "batch_id": "设备端生成的批次号",
  "expected_version": 3,
  "readings": [
    {"kind": "seepage", "value": 12.5, "observed_at": "2026-09-30T08:00:00Z"},
    {"kind": "displacement", "value": 3.1, "observed_at": "2026-09-30T08:05:00Z"}
  ]
}
```

- 读数按`observed_at`（现场观测时间）合并：观测时间早于已处置读数的补传照样入库留痕，生效读数取观测时间最新者，不按到达顺序覆盖。
- 每次成功补传后旧复核结论标记`superseded`并按生效读数重算，历史结论可经`GET /api/items/{id}/conclusions`查询，处置与审计记录全部保留。
- 同一缺陷并发补传只有一份成立，冲突方收到`409`及`current_version`，按当前版本重试即可。
- 批次整体事务提交，中途失败不留半条记录；用原`batch_id`重试幂等，已成功的批次重放返回原结果（HTTP 200），首次成功为201。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
