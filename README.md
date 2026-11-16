# A股自动化 · 选股 / 资讯 / 个股查询

> 量化选股、盘中观察池、早晚资讯与个股查询，GitHub Actions 自动运行并推送飞书。
> 准点由 [Cloudflare Worker](cloudflare/README.md) 触发，GitHub cron 只作兜底；所有时间为北京时间。

> ## ⚠️ 安全提醒
> 本仓库曾在 `config/config.yaml` 中提交过真实飞书 Webhook。当前文件已清空，但 **Git 历史仍可被读取**。
> 请删除并重建对应群机器人，把新 Webhook 写入 Secrets；不要把任何密钥写进被跟踪的文件。
> 另：本轮审计中提供的 GitHub PAT（`repo` + `workflow` 权限）已出现在对话里，应视为泄露并轮换。

## 快速开始

1. Fork / Clone 仓库，务必包含 `.github/workflows/`；首次运行前执行 `python main.py init`（约 30~45 分钟）。
2. 飞书群添加自定义机器人；若开启加签，记录加签密钥。
3. 在 **Settings → Secrets and variables → Actions** 配置：

| Secret | 必填 | 用途 |
| --- | --- | --- |
| `FEISHU_WEBHOOK` | 是 | 业务机器人 Webhook |
| `FEISHU_SECRET` | 否 | 业务机器人加签密钥 |
| `ALERT_WEBHOOK` | 建议 | 独立告警机器人，避免业务通道故障时告警一起哑掉 |
| `ALERT_SECRET` | 否 | 告警机器人加签密钥，未配时回落 `FEISHU_SECRET` |

4. 手动 Run `intraday-selection.yml` 或 `selection.yml`（可勾 `skip_update` 快速验证推送）。

## 运行节奏

| 北京时间 | 任务 | Workflow |
| --- | --- | --- |
| 08:13（每天） | 早间新闻 | `morning-news.yml` |
| 12:53（交易日） | 盘中强势观察池 | `intraday-selection.yml` |
| 19:33（交易日） | 收盘选股（15 种策略）+ 复盘 | `selection.yml` |
| 21:53（每天） | 晚间新闻 | `evening-news.yml` |
| 周二 01:03 | 仓库心跳 | `keep-alive.yml` |

> cron 使用 UTC：`UTC = 北京时间 − 8h`。每个业务任务另有 2~3 档错开的兜底 cron，靠「档期 + 台账」判重；
> 改主档时间必须同步 workflow 的 `schedule`、`EXPECT_HHMM` 与 Worker 的 `PLAN`。

## 本地开发

```bash
pip install -r requirements.txt
python intraday_selection.py --dry-run     # 只打印卡片，不推送
python scripts/alert.py --check            # 只读校验 webhook / 加签配置
python -m unittest discover -s tests
```

## 目录结构

- `main.py` / `intraday_selection.py`：收盘主程序与盘中轻量脚本
- `strategy/`：15 种选股策略；`trading/`：回测、评分、交易计划
- `utils/`：数据源、飞书推送、幂等台账；`web/`：管理界面
- `config/`：配置与模板；`.github/workflows/`：定时任务
- `cloudflare/`：统一准点调度 Worker

## 安全说明

- Webhook / 加签密钥只走 Secrets 或环境变量；`config/config.yaml` 中 `feishu.webhook_url` 必须留空。
- 加签要么两个机器人都开，要么都不开；只开一半会让业务推送与告警同时静默。
- 未配置 Webhook 会让任务判红，绝不允许「运行页绿色但群里没消息」。
- Worker 运维端点优先用 `X-Admin-Token` 请求头，避免 `?key=` 进入日志与浏览器历史。

## 排错

- **19021**：机器人开启加签但密钥未配或配错。
- **卡片内容与日期不符**：cron 常延迟数小时；任务按行情时间戳记档期，不按运行日期。
- **Actions 绿但没消息**：检查 `FEISHU_WEBHOOK` 是否配置、机器人是否被重置。

## 免责声明

本工具仅用于研究与复盘，不构成投资建议。市场有风险，投资需谨慎。

## License

MIT；衍生作品来源与致谢见 [LICENSE](LICENSE) 与 [notice.md](notice.md)。

## 详细文档

- [完整说明与排错](docs/README-full.md)
- [Cloudflare Worker 统一调度器](cloudflare/README.md)
- [档期一致性检查](.github/workflows/schedule-audit.yml)
