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

### 状态说明

| 状态 | 含义 |
| --- | --- |
| `suppressed` 抑制中 | 检修内触发，暂缓通知 |
| `pending` 待送 | 已安排投递，含退避等待或待按号核实（`needs_verify`） |
| `delivering` 待送（投递中） | 工作线程正在投递 |
| `sent` 已送 | 通知端已确认受理 |
| `skipped` 未补送 | 检修截止/结束前已恢复，按规则不补送 |
| `cancelled` 已取消 | 恢复时投递尚未开始，取消本轮 |

## 通知端故障注入

```bash
# normal | drop_receipt（已受理但回 504，回执丢失）| reject（未受理，回 503）
curl -X POST localhost:8081/api/fault -H 'content-type: application/json' \
     -d '{"mode":"drop_receipt"}'
curl localhost:8081/api/notifications      # 已受理的固定通知号
```

`NOTIFIER_MODE` 环境变量可设置初始模式。

## 自测覆盖

`pytest -q`（23 项）：

- 检修外触发立即送达，重复 tick 不重复通知；
- 检修内触发抑制、截止前恢复 `skipped` 且不补送；
- 检修到期后活跃告警按固定通知号送达，恢复后保留记录；
- 手动结束检修释放抑制告警；
- 通知端 503 时保持 `pending`、无 `accepted_at`，恢复后仅送达一次；
- 丢回执（504）按号核实为已受理，通知端 `deliveries` 仍为 1；
- 核实也不可达时保持待核实，不误报已送；
- 重启后残留投递先核实、不重发（通知端计数仍为 1）；
- 恢复后未开始投递取消；已开始投递保留并只核实；
- 恢复后停止对明确未受理轮次的退避重试；
- 调整检修截止时刻后，抑制轮次按新截止送达；
- 服务停机跨过截止时刻，重启后仍按时送达；
- 手动结束检修时已恢复轮次不补送；
- 通知端全程不可达时不误报已送，恢复后按号核实未受理则取消；
- API 参数校验、409 冲突、页面与静态资源、通知端故障模式与幂等。

## 已知限制

- 单设备、单实例假设：用进程内锁 + SQLite 串行化，未做多副本协调。
- 「投递已开始」以发出 HTTP 请求前写入 `delivery_started_at` 为准；
  通知端慢响应期间恢复会走「只核实」路径，而不是直接取消。
- 通知端是演示替身，受理记录与故障模式同样存 SQLite；生产替换时需保留
  按通知号查询的幂等语义。
- 未做鉴权、HTTPS、告警分级与真实设备采集；页面按 1 秒轮询 `/api/state`。
