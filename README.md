# 天文瞬变事件警报与后续观测

只使用Python标准库和SQLite的模块化服务，默认端口`8338`。支持全天巡天来源、候选事件去重、坐标与亮度测量合并、离线攒批与重试、优先级计算与重算、观测申请、望远镜排程、撤回、重分类、修正、观测队冲突、台站权限和审计。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、领域异常、数据对象和时间工具。
- `src/rules.py`：状态机、优先级、测量合并、台站归属和排程冲突约束。
- `src/repository.py`：SQLite持久化、乐观锁、幂等和审计查询。
- `src/service.py`：用例编排、权限校验和版本控制。
- `src/http_api.py`：JSON接口和统一错误响应。
- `src/audit.py`：操作审计。
- `static/index.html`：最小演示页面。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8338
```

## 核心对象

`source`为巡天来源，`candidate`为瞬变候选，`telescope`为望远镜，`observation`为后续观测申请。

## 台站与测量合并

观测员通过`X-Station-Id`请求头声明所属台站。提交测量时，测量的`station_id`必须与观测员所属台站一致，越权提交返回`403 PermissionDenied`（管理员可代任意台站提交）。

夜间断网时，台站把多次测量攒在本地；网络恢复后，对候选事件调用`merge_batch`动作一次性提交。批量合并会逐条校验：合法测量追加到`measurements`，非法或重复的测量保留在`pending_measurements`中并记录原因，不会丢失。

- `merge_batch`：批量合并，返回`merged_count`/`rejected_count`，被拒测量进入`pending_measurements`。
- `retry_pending`：重试待处理测量；已被其他批次合并的重复项自动丢弃，仍不合法的保留并累加尝试次数。
- `discard_pending`：显式清空待处理测量。
- `recalculate_priority`：根据最新测量重新计算优先级。

`merge_batch`支持可选`batch_id`幂等键：相同`batch_id`重复提交不会重复合并，直接返回首次结果。

## 并发与一致性

测量合并使用乐观锁：提交时携带`expected_version`（或由服务端读取当前版本）。两台站同时合并同一候选事件时，后到的提交遇到版本冲突返回`409 ConflictError`，重试后双方测量都保留，不会互相覆盖。

候选事件有活跃（已申请或已排定）观测时，撤回候选会被拒绝，避免已排定的观测申请被悄悄孤立。

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`过滤
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/entities/<id>/actions`
- `GET /api/audit`

身份通过`X-User-Id`、`X-Role`和`X-Station-Id`请求头传入。可选`Idempotency-Key`防止重复创建。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

轨道和观测计划使用简化的字符串时间窗比较，不包含真实天文历表、可见性预报、望远镜控制系统和观测数据存储。
