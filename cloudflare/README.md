# Cloudflare Worker 统一准点调度器

## 为什么需要它

GitHub Actions 的 `schedule` 只保证「尽力而为」。Wind866 账号实测 34 次定时运行
**0 次准时**，延迟 3.3~9.7 小时，且高负载时会直接丢弃排队的档期。延迟发生在
GitHub 调度器侧（run 创建 → job 启动仅 3~4 秒），改 cron 分钟数解决不了。

所以准点触发交给外部定时器，仓库里的 cron 退化为兜底：

```
Cloudflare Worker  准点 ──┐
                          ├─→ workflow_dispatch → 推送脚本 → 飞书
GitHub cron   延迟数小时 ─┘        ↑
                                   └─ utils/push_guard.py 台账守卫：
                                      只有飞书真的推送成功才记档期，
                                      后到的那次读台账后秒级退出
```

本 Worker **一个脚本管 3 个仓库、9 个档期**：

| 北京时间 | 目标 |
| --- | --- |
| 08:43 每天 | BIGApush / 早间新闻 |
| 13:07 工作日 | limit-up-relay / 盘中快照 |
| 13:13 工作日 | BIGApush / 盘中选股 |
| 15:37 工作日 | limit-up-relay / 收盘定稿 |
| 15:43 工作日 | BIGApush / 收盘选股 |
| 20:00 工作日 | etf-feishu-push / ETF 资金流向 |
| 22:13 每天 | BIGApush / 晚间新闻 |
| 周一 09:00 | BIGApush + limit-up-relay / 数据源自检 |
| 周二 01:00 | BIGApush / Keep Alive |

> Cloudflare 免费版**整个账号只有 5 个 Cron Trigger**，逐个档期配 cron 装不下。
> 这里用单个 `* * * * *` 每分钟唤醒一次，代码里换算成北京时间查表命中，
> 只占 1 个额度；每天 1440 次调用，远低于免费版 100,000 次/天，
> 未命中的分钟不做任何网络请求。

## 一次性配置

### 1. 建 GitHub Token

<https://github.com/settings/personal-access-tokens/new>（Fine-grained）：

- **Repository access** → Only select repositories → 勾 `BIGApush`、`limit-up-relay`、`etf-feishu-push`
- **Permissions** → Repository permissions → **Actions: Read and write**（其它保持 No access）
- **Expiration** → 选最长（**366 天**），并加日历提醒到期前轮换

GitHub 官方允许 fine-grained PAT 最长 1 年或无到期；不要按旧文档的 90 天设置，
否则到期后所有准点触发会静默失效。Worker 每天 08:00 会自检 token，失效会告警。

### 2. 建独立告警机器人

在飞书群里**另建一个**自定义机器人，拿到 webhook。业务推送的 webhook 一旦被移除或重置，
失败告警也会发不出去 —— 所以告警通道必须独立。

### 3. 部署 Worker

1. <https://dash.cloudflare.com> → Workers & Pages → Create → Worker → 名字 `gh-actions-scheduler` → Deploy
2. Edit code → 粘贴 `cloudflare/worker.js` 全部内容 → Deploy
3. Settings → Variables and Secrets：

| 名称 | 类型 | 值 |
| --- | --- | --- |
| `GH_TOKEN` | **Secret** | 第 1 步的 `github_pat_...` |
| `ALERT_WEBHOOK` | **Secret** | 第 2 步的告警机器人 webhook |
| `OWNER` | Text | `Wind866` |
| `BRANCH` | Text | `main` |

4. Settings → Triggers → Cron expression 填 `* * * * *`

或用 wrangler：

```bash
cd cloudflare
npx wrangler deploy
npx wrangler secret put GH_TOKEN
npx wrangler secret put ALERT_WEBHOOK
```

### 4. ⚠️ 删掉旧的触发器

如果之前部署过只管晚间新闻的 `biga-push-cron`（cron `13 14 * * *`），
**必须先删除它的 cron 或整个 Worker**，否则 22:13 会被两个 Worker 各触发一次。
免费账号总共只有 5 个 cron 额度，也别浪费。

### 5. 自测

```
https://gh-actions-scheduler.<子域>.workers.dev/health            查看档期表与配置
https://gh-actions-scheduler.<子域>.workers.dev/health?check=1     校验 GH_TOKEN
https://gh-actions-scheduler.<子域>.workers.dev/test?at=22:13      手动打一发指定档期
```

根路径**不会**触发任何任务（旧版一打开就全量触发，已修正）。
打完去 Actions 看日志：出现 `⏭ 跳过本次推送：档期 ... 已在 ... 推送成功` 说明
幂等守卫生效；想真的再推一次，在 Actions 手动 Run workflow 勾 `force`。

### 6. 核对线上版本（重要）

`/health` 会返回 `build` 字段，例如 `"build": "2026-11-14.1"`。**每次改完 `worker.js`
都要重新 Deploy，并用这个字段确认线上跑的就是仓库里这一版**。曾经出现过线上版本
与仓库不同步：线上没有 `/test` 路由、档期名也不一致，导致自测请求被当成根路径处理，
排查时极易误判成「Worker 挂了」。

同时确认 Settings → Triggers 里 Cron expression 是 `* * * * *`（面板显示 `Every minute`）。
`/health` 只能证明 Worker 已部署，**证明不了定时触发器已生效**。

### 7. 两个可靠性修复（2026-11-14）

- **用 `event.scheduledTime` 而不是 `Date.now()` 匹配档期**：Worker 冷启动或 CF 调度
  抖动会让实际执行落进下一分钟，而档期是精确到 `HH:MM` 的字符串比对，一跨分钟
  就永久错过该档期。这是配置齐全却仍漏跑的根因。偏差超过 2 分钟会主动告警。
- **每 5 分钟补档回看**：若某次唤醒被 CF 丢弃，会在 2~7 分钟后检查该档期是否在
  Actions 留下过 `workflow_dispatch` 记录，没有则补触发。判重窗口 ±10 分钟，
  正常触发过的档期不会重复发；查不到 API 时一律不补（宁可漏补也不重复推）。
  不需要 KV / D1，仍是无状态 Worker。

### 8. 每日自动对账

`scripts/schedule_audit.py` + `.github/workflows/schedule-audit.yml` 每天北京 23:30
把「当日 9 档期应到 / 实到 / 延迟分钟」生成成 Markdown，写进 Step Summary 并上传
artifact；发现已覆盖范围内有漏跑就让任务失败、走告警。

内置 `GITHUB_TOKEN` 只能读本仓库，要核对另外两个仓库（尤其私有的 `etf-feishu-push`）
需在 BIGApush 配置 Secret `AUDIT_PAT`（fine-grained PAT，三仓库 `Actions: read`）。
未覆盖的仓库标「⬜ 未覆盖」，**不判失败**，避免把「查不到」误报成「没跑」。

## 仓库侧配套改动（已随本次提交落地）

- 所有推送脚本接入台账式幂等守卫 `utils/push_guard.py` / `src/push_guard.py`：
  只有飞书推送成功才写 `.push-state/ledger.json`，非交易日跳过 / 迟到跳过 / 调试运行
  不再被误判成「已推送」。
- 盘中任务的「迟到 ≥90 分钟就不推」改为**只标注不拦截**：外部准点触发时由台账判重，
  外部定时器未部署时，迟到的一条也比完全没有强。
- 每个仓库的 `ALERT_WEBHOOK` 是可选 Secret；未配置时失败告警自动退回业务 webhook。
  建议按第 2 步配上，才能真正避免「webhook 挂了却没人知道」。
- `etf-feishu-push` 已有基于 `history.json` 的交易日判重，无需台账。

## 额度与限制

- 免费版 100,000 请求/天、每账号 5 个 Cron Trigger：本方案占 1 个、约 1440 次/天
- Worker 无状态，不需要 KV / D1 / 任何存储；档期去重靠查 Actions API（3 分钟窗口）
- CF cron 极端情况可能晚几秒到几分钟，与 GitHub 的「几小时」不是一个量级
- A 股节假日不做硬编码，由各脚本自己的交易日探测跳过
