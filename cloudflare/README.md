# Cloudflare Worker 准点触发器

## 为什么要有这个

GitHub Actions 的 `schedule` **不保证准点**。BIGApush 实测：33 次定时运行，**0 次准时**，
延迟 3h17m ~ 9h43m。延迟发生在 GitHub 调度器侧（不是 runner 排队——run 创建到 job 启动只有 3~4 秒）。

所以改成「外部准点触发 + 仓库 cron 兜底」：

```
Cloudflare Worker  22:13 准点 ─┐
                              ├─→ workflow_dispatch → evening_news.py
GitHub cron        14:13 UTC ─┘（延迟数小时才到，兜底用）

evening_news.py 里的 utils/push_guard.py 会查「本档期是否已推送成功」，
先到的那个真的推，后到的直接跳过 → 不会重复推送。
```

## 一次性配置（约 10 分钟）

### 1. 建 GitHub Token

打开 <https://github.com/settings/personal-access-tokens/new>（Fine-grained）：

- **Repository access** → Only select repositories → 只勾 `BIGApush`
- **Permissions** → Repository permissions → **Actions: Read and write**
  （其它全部保持 No access；`Metadata` 会自动带上 Read）
- **Expiration** → 90 days（到期前换新）

生成后复制 `github_pat_...`，**只显示一次**。

### 2. 建 Worker

1. 打开 <https://dash.cloudflare.com> → **Workers & Pages** → **Create** → **Worker**
2. 名字填 `biga-push-cron` → **Deploy**（先用默认 hello world）
3. 点 **Edit code** → 删掉模板，粘贴 `cloudflare/worker.js` 全部内容 → **Deploy**

### 3. 配变量

Worker 详情页 → **Settings** → **Variables and Secrets**：

| 名称 | 类型 | 值 |
| --- | --- | --- |
| `GH_TOKEN` | **Secret**（点 Encrypt） | 第 1 步的 `github_pat_...` |
| `REPO` | Text | `Wind866/BIGApush` |
| `WORKFLOWS` | Text | `evening-news.yml` |
| `BRANCH` | Text | `main` |

改完点 **Deploy** 让变量生效。

### 4. 配定时

**Settings** → **Trigger Events** → **Add** → Cron expression：

```
13 14 * * *
```

⚠️ **Cloudflare 的 cron 一律是 UTC**，`13 14 * * *` = 每天 UTC 14:13 = **北京时间 22:13**。
（夏令时与中国无关，全年不变。）

### 5. 自测

浏览器直接打开 Worker 域名：

```
https://biga-push-cron.<你的子域>.workers.dev/
```

返回 `✅ evening-news.yml 已触发` 就说明通了。然后去仓库 Actions 看最新一次「晚间新闻」运行：

- 日志里出现 `⏭ 跳过本次推送：档期 2026-11-13 已在 ... 推送过` → **完全正确**，说明幂等守卫在起作用
- 想真的推一次：在 Actions 里手动 Run workflow，勾上 `force`

## 用 wrangler 部署（可选）

不想点网页的话：

```bash
cd cloudflare
npx wrangler deploy
npx wrangler secret put GH_TOKEN      # 粘贴 PAT
```

`wrangler.toml` 里已经写好了 vars 和 cron，改名后直接 deploy 即可。

## 以后要接管其它 workflow

`WORKFLOWS` 支持逗号分隔，例如 `evening-news.yml,morning-news.yml`。
**但先给对应脚本加上幂等守卫再开**，否则两个触发器会把同一条消息推两次：

```python
from utils.push_guard import should_skip
if should_skip("morning-news.yml", force=...):
    return
```

各脚本的档期 cutoff 不一样：晚间用默认 8 点；早间（08:43）用 `cutoff_hour=12`；
收盘选股（15:43）保持默认即可。

## 免费额度与限制

- 免费版 **100,000 次请求/天**，这里一天 1 次
- 免费版允许每个 Worker 配置多个 cron trigger，够用
- Worker 本身无状态，不需要 KV / D1 / 任何存储
- cron 触发在极端情况也可能延迟几秒到几分钟，但和 GitHub 的「几小时」不是一个量级
