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

## 飞书机器人：群里发股票代码查个股

除了「准点触发」，本 Worker 还兼了一个入口：群里发 `600519`，机器人回该股的深度分析。

```
群里 @机器人 发「600519,000001」
      ↓ Worker 每 2 分钟主动调「获取会话历史消息」   ← 不需要任何公网回调地址
      ↓ 筛出「@了机器人」且含 6 位数字的消息
      ↓ 先回一句「已收到，约 1~2 分钟」
      ↓ dispatch BIGApush/stock-query.yml（带 inputs.stocks 和 chat_id）
GitHub Actions 跑真实取数 → 结论推回提问的那个群
```

> **为什么不用事件订阅（回调）**
>
> 回调要求一个国内可访问的 HTTPS 地址，而 `*.workers.dev` 实测全国 200+ 节点全部超时
> （只有港澳台通）。飞书的回调服务器在国内机房，请求根本发不出来，表现为
> 「请求 3 秒超时」或「url invalid」。换成自建域名能解决，但要买域名、改 NS、等生效。
>
> 轮询是反过来的：Worker 每分钟本来就被 cron 唤醒，顺手调一次飞书接口读消息。
> **不依赖任何入站请求**，因此不需要域名、证书、事件订阅，也不会撞上 3 秒超时。
> 代价是**最多延迟 2 分钟**（`POLL_EVERY_MIN`）。

### 为什么必须用自建应用

群里的**自定义机器人（webhook）只能发、不能收** —— 它收不到你发的消息，
所以「群里输入代码」用 webhook 做不出来。双向只能走自建应用的事件订阅。

### 一次性配置

**1. 开放平台里开权限**

<https://open.feishu.cn/app> → 选应用 → 权限管理 → 开通：

| 权限 | 用途 |
| --- | --- |
| `im:message` **或** `im:message:readonly` **或** `im:message.history:readonly` | **轮询读群消息必需**（三者任一即可，实测报错会列出这三个） |
| `im:message:send_as_bot` | 以机器人身份发回执 |
| `im:chat:readonly` | 列出机器人所在的群；不开会静默返回 0 个群 |

开完要**发布版本**（创建版本 → 申请发布），权限才生效。只加权限不发布 = 仍然 99991672。

**2. 打开机器人能力**

应用功能 → 机器人 → 开启；然后把这个机器人**拉进目标群**（群里 @ 它或从群设置添加）。

**3. 事件订阅：轮询模式下不需要，可整节跳过**

只有在你自己有域名、并把域名绑到 Worker 之后才值得配：

- 事件与回调 → 订阅方式选「将事件发送至开发者服务器」
- 请求地址：`https://<你的域名>/feishu`（不要用 `*.workers.dev`，国内不通）
- 添加事件：`接收消息 im.message.receive_v1`

保存时飞书会打一发 `url_verification`，本 Worker 原样回 `challenge`，保存成功即地址通。
加密策略保持默认不填最省事；若开了，需同步 `npx wrangler secret put FEISHU_ENCRYPT_KEY`。

轮询与回调**可以同时开着**，`message_id` / `event_id` 各自去重，不会重复触发分析。

**4. 给 Worker 配 Secret**

```bash
npx wrangler secret put FEISHU_APP_ID        # cli_ 开头
npx wrangler secret put FEISHU_APP_SECRET
npx wrangler deploy
```

用 Dashboard 手工部署的话：Worker → Edit code → 粘贴 `cloudflare/worker.js` → Deploy，
再到 Settings → Variables and Secrets 里加上面两个（类型选 Secret）。

### 行为说明

- 只认**文本消息里的 6 位连续数字**；闲聊不回应（避免机器人在群里刷屏）
- **必须 @ 机器人**：群里别人发的 6 位数字（订单号、金额）不会被当成股票代码
- 轮询按 `message_id` 去重，同一条消息只处理一次；回调按 `event_id` 去重
- **去重分两层**：实例内的 `Map` + Cache（30 分钟）。cron 每分钟唤醒，但 Cloudflare
  不保证复用实例，冷实例的 `Map` 是空的 —— 只靠内存去重时同一条消息会被前后两个
  实例各扫一遍，群里出现**两条「已收到」**、Actions 跑两遍推两张图。Cache 跨实例
  存活，能挡住这种重复（现象与排查见文末「两条已收到」）
- 一次最多 6 只，超出部分丢弃并在回执里体现
- 同一组代码 90 秒内只触发一次；飞书重投的同一 `event_id` 直接忽略
- 非文本消息（图片/文件）回一句「只认文本股票代码」
- 失败会回「触发失败，GH_TOKEN 可能已过期」，不会静默
- 结果回到**提问的那个会话**（Worker 把 `chat_id` 传给 workflow）；万一按会话发送失败，
  自动退回 `FEISHU_WEBHOOK` 绑定的群，不会石沉大海
- 因此仓库侧也要配两个 Secret 才能「哪问哪答」：`FEISHU_APP_ID` / `FEISHU_APP_SECRET`
  （与 Worker 用的是同一个自建应用）。只配 webhook 时，结果固定发到那个群

### 自检

**先跑这条**，报告会直接显示在浏览器里，比看日志快：

```
https://<域名>/feishu/poll
```

正常长这样（`扫描群数` 为 0 说明机器人还没被拉进群）：

```
机器人：BIGApush open_id=ou_xxxx
时间窗口：最近 90 秒（轮询间隔 1 分钟）
扫描群数：1
  oc_xxxxxxxx 最近 20 条（最新一条 12 秒前），落在窗口内 1 条
    · om_xxxxxxxx 12s前 text/user 提及=ou_xxxx 代码=600519 → 触发 600519
本次处理 1 条查询
```

`·` 那行是**每条消息的判定结果**，跳过会直接写明原因，不用再猜：

| 判定 | 含义 |
| --- | --- |
| `触发 600519` | 命中，已回执并派发 Actions |
| `跳过（没@机器人）` | 群里的 6 位数字不 @ 机器人不会被当股票代码，防止误触发 |
| `跳过（类型 image）` | 只认 `text`（以及富文本 `post`），图片/文件不回 |
| `跳过（文里没有 6 位代码）` | @ 了但正文里没有股票代码 |
| `跳过（机器人自己发的）` | 不回自己的消息，避免死循环 |
| `已在上一轮处理过` | 同一实例里 `message_id` 去重生效，正常现象 |
| `已被别的实例处理过，本次跳过` | 冷启动的第二个实例扫到同一条消息，Cache 去重挡住了，正常现象 |

命中之后还有一行 `↳`，说明这一单到底有没有派出去：

```
      ↳ 已派发 002163
      ↳ 已派发 002163（回执没发出去：230035 ...）      ← 任务照跑，只是群里没回执
      ↳ 派发失败：HTTP 401 ...（下一轮重试）            ← 没派出去，不会记去重，下一分钟重试
      ↳ 刚查过，跳过派发 002163                         ← 90 秒冷却内重复 @，只回一句提示
      ↳ 这条已被另一个实例处理，跳过（跨实例去重生效）  ← 冷实例重复扫到，不会重复回执/派发
```

常见报错对应：

| 报错 | 原因与解法 |
| --- | --- |
| `im/v1/chats 失败 99991672` | 权限没开或没发布版本（`im:chat:readonly`） |
| `im/v1/messages 失败 99991672` | 缺 `im:message` / `im:message:readonly` / `im:message.history:readonly` 之一；报错正文会列出这三个 |
| `bot/v3/info 失败 ...` | `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 配错 |
| `扫描群数：0` | 机器人不在任何群里，先拉进群 |
| **`230035 Send Message Permission deny`** | **群没给机器人发言权限**：群设置 → 群管理 →「谁可以发消息」改成「所有群成员」，或把机器人设为群管理员。注意这是**群**的设置，不是开放平台权限，跟 `im:message:send_as_bot` 无关（单聊能发、群里发不出就是它） |
| `派发失败：HTTP 401 / 403` | `GH_TOKEN` 过期或缺 `workflow` 权限，去 `/health?check=1` 看 `tokenValid` |
| `派发失败：HTTP 404` | 派发 URL 拼错（漏 `/actions/workflows/`）或 workflow 文件名不对。`/feishu/selftest` 里有一行 `个股查询 workflow：OK/失败` 专查这个 |

还有一个通道自检端点，逐项验完再决定要不要真发一条：

```bash
curl https://<域名>/feishu/selftest          # 飞书 token / 机器人 / 目标群 / GH token / workflow 可达性
curl https://<域名>/feishu/selftest?send=1   # 再多做一步：真往群里发一条自检消息
```

`?send=1` 会真的发消息，专门用来一眼看出是「群发言权限（230035）」还是别的问题。

```bash
curl https://<域名>/health            # build 字段应为 2026-11-14.10 或更新，确认线上跑的是这版
curl https://<域名>/feishu/poll       # 立刻轮询一次，输出扫描报告
```

线上版本不对是最常见的「代码改了没生效」原因，`/health` 的 `build` 就是为此加的。

### 排错：「群里收到两条『已收到』」

现象：只 @ 了一次机器人，群里却出现两条一模一样的 `🔍 已收到 ...`，`Actions` 也跑了
两遍（推了两张图）。

原因不在飞书重投，而在**冷启动**：

1. cron 每分钟唤醒一次，Cloudflare **不保证**把下一分钟交给同一个实例；
2. 冷实例的 `pollSeen` / `seenEvents` / `notifyAt` 全是空的；
3. 冷启动回看 90 秒 > 轮询间隔 60 秒，所以每个新实例都会把上一分钟那条消息再扫一遍。

只靠内存去重就会「各回执一次、各派发一次」。现在的做法是把去重记在 **Cache API**
（`msg:<message_id>` / `rcpt:<会话:代码>` / `q:<会话:代码>`，TTL 30 分钟），同 colo 内
跨实例存活：第二个实例查到时直接返回「已被别的实例处理过」，群里一声不出。

对应回归测试：`node tests/test_worker_dedupe.mjs`（CI 会跑）。它用「同一份 Cache +
两份独立模块实例」精确模拟两次冷启动。
