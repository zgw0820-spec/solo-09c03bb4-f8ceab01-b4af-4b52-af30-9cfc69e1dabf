# 供电柜告警检修抑制台

单设备（供电柜 #1）过热告警的检修抑制与可靠送达演示。FastAPI + SQLite，
两个容器：告警服务（网页 + API）与本地通知端（可模拟「已受理但丢回执」等故障）。

## 启动

```bash
docker compose up --build
```

- 操作页：<http://localhost:8080>
- 通知端（仅观测/注入故障）：<http://localhost:8081>

本地不装 Docker 时也可直接运行（需先 `pip install -r requirements-dev.txt`）：

```bash
NOTIFIER_DB=/tmp/notifier.db uvicorn app.notifier.main:app --port 8081
ALARM_DB=/tmp/alarm.db NOTIFIER_URL=http://127.0.0.1:8081 uvicorn app.alarm.main:app --port 8080
pytest -q
```

SQLite 文件分别落在两个具名卷 `alarm-data`、`notifier-data` 的 `/data` 下，
容器重启/重建后数据仍在。

## 业务规则与实现位置

| 需求 | 实现 |
| --- | --- |
| 设置检修截止时刻（可随时调整） | `POST /api/maintenance`，截止前再设置即顺延同一窗口 |
| 检修内新触发告警先不通知 | 触发时落在 `suppressed`，不产生任何投递 |
| 到期前恢复的不补送 | `recover()` 置 `skipped`，之后截止事件也不会再捞起它 |
| 到期仍活跃的按本轮固定通知号送达 | 截止时 `suppressed → pending`，通知号在建轮时即固定为 `ALM-000001` 形式 |
| 检修外触发的正常通知 | 触发即 `pending`，工作线程立即投递 |
| 恢复前已受理的通知保留记录 | `sent` 状态在恢复后不变，只追加说明；`accepted_at` 保留 |
| 恢复后尚未开始的投递取消 | `recover()` 见 `delivery_started_at IS NULL` 即 `cancelled` |
| 回执丢失先按通知号核实 | 任何非确认回执都 `GET /notifications/{no}` 核实，核实到受理即 `sent` |
| 未受理不能误报已送 | 只有通知端明确 `accepted:true` 才写 `accepted_at`；拒绝则退避重试 |
| 故障或重启后接续 | 启动时把残留 `delivering` 行改为「先核实」；通知端按通知号幂等 |
| 不重复实际通知 | 同一轮固定一个通知号，通知端对同号只记一条（`deliveries` 计数可观测） |
| 逐轮清理两端记录 | `POST /api/rounds/{id}/cleanup`：告警端删行，通知端删对应受理记录 |
| 只清理已恢复且结果确定的轮次 | `cleanup_block_reason()`；活跃/待送/待核实为 409 |
| 清理失败不会误报正常已送 | 行停在 `cleaning` 并记 `cleanup_error`，页面红色提示、不计数为 `sent` |
| 清理可重试直至完成 | 同端点或 `/cleanup/retry`；两端删除均幂等；重启保留 `cleaning` |
| 清理中的旧轮不再投递 | `cleaning` 不进入 `_claim_next_locked`，worker/tick 都不会再发 |
| 不误删同号异轮记录 | 通知端 `clean` 要求 `round_id` 与存储一致，否则 409 且保留原记录 |
| 已清理通知号不复用 | 两端各留 `retired/cleaned_notifications` 墓碑；新轮号跳过已用号 |

### 清理流程

1. 值班员在「告警轮次」表点「清理」，仅 `sent` / `skipped` / `cancelled`
   且已恢复、`needs_verify=false`、`sent` 有 `accepted_at` 的行可点。
2. 告警端先置 `cleaning`（原状态记入 `prior_state`），再调用通知端
   `POST /notifications/{no}/clean?round_id=...`。
3. 通知端按 `notification_no + round_id` 精确删除，写墓碑并返回 200；
   同号异轮返回 409，记录**不删**。
4. 成功则告警端写墓碑并删除该轮；失败（失联/409/未知）保留行，
   显示「清理未完成」与原因，恢复后重试，直到成功。
5. 两端删除幂等：重复重试、通知端已删而本地重启等场景都不会重复处理或
   把未完成当完成。

### 状态说明

| 状态 | 含义 |
| --- | --- |
| `suppressed` 抑制中 | 检修内触发，暂缓通知 |
| `pending` 待送 | 已安排投递，含退避等待或待按号核实（`needs_verify`） |
| `delivering` 待送（投递中） | 工作线程正在投递 |
| `sent` 已送 | 通知端已确认受理 |
| `skipped` 未补送 | 检修截止/结束前已恢复，按规则不补送 |
| `cancelled` 已取消 | 恢复时投递尚未开始，取消本轮 |
| `cleaning` 清理中 | 值班员发起清理；通知端删除未确认前保留该行并标出未完成 |

## 通知端故障注入

```bash
# normal | drop_receipt（已受理但回 504，回执丢失）| reject（未受理，回 503）
curl -X POST localhost:8081/api/fault -H 'content-type: application/json' \
     -d '{"mode":"drop_receipt"}'
curl localhost:8081/api/notifications      # 已受理的固定通知号
```

`NOTIFIER_MODE` 环境变量可设置初始模式。

## 自测覆盖

`pytest -q`（42 项）。原有 23 项覆盖检修抑制、可靠送达、丢回执核实、
重启接续与 API 校验；新增 `tests/test_cleanup.py` 及 API 用例覆盖：

- 已恢复 `sent` 轮次清理后两端记录均删除，重复清理幂等；
- `skipped` / `cancelled` 可清理；活跃、待送、待核实轮次清理返回冲突；
- 通知端失联时清理标记未完成，页面状态为 `cleaning` 且不计入已送，恢复后重试完成；
- 清理未完成期间反复 tick 不重复投递，重启后标记仍在、仍不投递；
- 重启跨过「通知端已删、本地未删」之间：重试可完成且不重复删除；
- 通知端同号异轮记录在清理时返回 409 并保留，不会被误删；
- 清理后通知号退役，新轮分配新号；通知端拒收已退役号；
- 清理中仍可触发新告警，新告警不会复用旧号；
- 丢回执后已按号确认的轮次可正常清理。

另用真实 uvicorn 双进程 + SQLite 文件做过端到端自测：送达→清理、通知端
停机清理→重启告警端→恢复通知端→重试完成、同号异轮保护、检修抑制与
手动结束照常送达，均通过。

## 已知限制

- 单设备、单实例假设：用进程内锁 + SQLite 串行化，未做多副本协调。
- 「投递已开始」以发出 HTTP 请求前写入 `delivery_started_at` 为准；
  通知端慢响应期间恢复会走「只核实」路径，而不是直接取消。
- 通知端是演示替身，受理记录与故障模式同样存 SQLite；生产替换时需保留
  按通知号查询的幂等语义与按 `round_id` 限定的删除接口。
- 清理是「告警端为源」的两段删除：通知端记录已删、本地尚未落库时进程若
  崩溃，重启后该轮仍显示清理未完成，需点一次重试收敛（通知端侧幂等）。
- 清理墓碑两端各自保留，用于禁止通知号复用；未提供墓碑的自动过期清理。
- 未做鉴权、HTTPS、告警分级与真实设备采集；页面按 1 秒轮询 `/api/state`。
