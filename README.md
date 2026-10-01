# 天文瞬变事件警报与后续观测

只使用Python标准库和SQLite的模块化服务，默认端口`8338`。支持全天巡天来源、候选事件去重、坐标与亮度测量合并（含台站夜间离线批次）、优先级计算、观测申请、望远镜排程、撤回、重分类、修正、观测队冲突、角色权限、台站归属和审计。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、领域异常、数据对象和带台站归属的操作主体。
- `src/rules.py`：状态机、优先级、测量合并、台站授权和排程冲突约束。
- `src/repository.py`：SQLite持久化、乐观锁、幂等和审计查询。
- `src/service.py`：用例编排、权限校验、版本控制和离线批次逐项合并。
- `src/http_api.py`：JSON接口和统一错误响应。
- `src/audit.py`：操作审计。
- `static/index.html`：最小演示页面。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8338
```

## 核心对象

`source`为巡天来源，`candidate`为瞬变候选，`telescope`为望远镜，`observation`为后续观测申请，`station`为观测台站，`measurement_batch`为台站断网期间攒在本地、联网后提交的测量批次。

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`过滤
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/entities/<id>/actions`
- `GET /api/audit`

身份通过`X-User-Id`和`X-Role`请求头传入；观测台站提交测量时另带`X-Station-Id`。可选`Idempotency-Key`防止重复创建。

## 离线测量与台站归属

夜间断网的台站把多次测量作为`measurement_batch`（字段`candidate_id`、`station_id`、`measurements[]`）提交，创建后对批次执行`submit`逐项合并：

- 每条测量带结果状态：`merged`（已并入）、`blocked`（因乐观锁竞争未并入，批次保留为`partial`，可`retry`）、`rejected`（坐标/亮度/时间非法，终态不再重试）。
- 两台站对同一事件同时提交时，先到的写入成功，后到的不会覆盖；批次可`retry`，已并入的项不会重复合并，审计逐条留痕。
- 去重键为`(station_id, observed_at)`：不同台站在同一时刻的观测是两条独立测量，只有同台站同刻才判重。
- 台站授权：`operator`必须在台站`observer_ids`名单内、台站处于`active`，且`X-Station-Id`与批次归属一致，否则返回403；`analyst`可不带台站合并，`admin`可代为操作。
- 最新一条测量的亮度变化时，候选优先级立即失效并重算（`priority_recalculated_at`）；更早的补测只补充历史，不会改写最新亮度与优先级。
- 测量合并只写`candidate`，已排定的`observation`（状态、版本、数据）不受影响。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

轨道和观测计划使用简化的字符串时间窗比较，不包含真实天文历表、可见性预报、望远镜控制系统和观测数据存储。
