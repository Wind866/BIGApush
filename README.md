# A股自动化 · 选股 / 资讯 / 个股查询

<p>
  <img alt="CI" src="https://img.shields.io/github/actions/workflow/status/Wind866/BIGApush/ci.yml?label=CI">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.11-3776AB">
  <img alt="License" src="https://img.shields.io/badge/License-see%20NOTICE-lightgrey">
  <img alt="Push" src="https://img.shields.io/badge/push-Feishu-00B0F0">
</p>

> 一套跑在 GitHub Actions 上的 A 股盘后流水线：**15 种形态策略收盘选股 + 盘中观察池 + 早晚资讯 + 个股查询**，结果渲染成飞书交互卡片推送。

所有时间均为北京时间。准点由 [Cloudflare Worker](cloudflare/README.md) 统一派发，仓库内 GitHub cron 只作兜底。

## 它做什么

- **收盘选股**：15 种形态策略（金叉、底部转折、涨停回踩、多方炮、W 底等）逐股打分，输出当日观察池与交易计划。
- **盘中观察池**：12:53 用轻量脚本跑一遍强势股，盘中就能看到候选，不必等到收盘。
- **早晚资讯**：08:13 / 21:53 两个档期分别汇总要闻，附带情绪与板块分布。
- **个股查询**：`stock-query.yml` 支持按代码批量查行情、资金流与基本面，直接回卡片。
- **档期对账**：`schedule-audit.yml` 每天核对「应到 / 实到」，漏跑直接判红，不会静默丢档。

## 快速开始

### 1. 拿到代码

```bash
git clone https://github.com/Wind866/BIGApush.git
cd BIGApush
```

必须包含 `.github/workflows/`。首次运行前需初始化数据库（约 30~45 分钟，见「本地开发」）。

### 2. 配置 Secrets

仓库 **Settings → Secrets and variables → Actions** 添加：

| Secret | 必填 | 用途 |
| --- | --- | --- |
| `FEISHU_WEBHOOK` | 是 | 业务机器人 Webhook |
| `FEISHU_SECRET` | 否 | 业务机器人加签密钥 |
| `ALERT_WEBHOOK` | 建议 | 独立告警机器人；业务通道故障时告警仍能发出 |
| `ALERT_SECRET` | 否 | 告警机器人加签密钥，未配时回落 `FEISHU_SECRET` |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 否 | 自建应用凭证，个股查询发图片/富文本时需要 |
| `AUDIT_PAT` | 否 | fine-grained PAT（四个仓库 Actions: read），档期对账跨仓库核对用；不配则只核本仓库，其余标「未覆盖」 |

### 3. 手动验证一次

**Actions → 收盘选股 → Run workflow**，可勾 `skip_update` 跳过漫长取数、只验推送链路。

## 运行节奏

| 北京时间 | 任务 | Workflow |
| --- | --- | --- |
| 08:13（每天） | 早间新闻 | `morning-news.yml` |
| 12:53（交易日） | 盘中强势观察池 | `intraday-selection.yml` |
| 19:33（交易日） | 收盘选股（15 种策略）+ 复盘 | `selection.yml` |
| 21:53（每天） | 晚间新闻 | `evening-news.yml` |
| 周二 01:03 | 仓库心跳（防 60 天无活动停用定时任务） | `keep-alive.yml` |

- cron 使用 UTC：`UTC = 北京时间 − 8h`。
- 每个业务任务另有 2~3 档错开的兜底 cron，靠「档期 + 台账」判重，补档不重复推送。
- **改主档时间必须同步三处**：workflow 的 `schedule`、脚本内的 `EXPECT_HHMM`、Worker 的 `PLAN`。`schedule-audit.yml` 会校验前者的一致性。

## 目录结构

```text
main.py                  收盘主程序（数据更新 + 选股 + 推送）
intraday_selection.py    盘中轻量脚本
daily_news.py            资讯采集（早晚共用）
stock_query.py           个股查询
sector_data.py           板块数据
strategy/                15 种形态策略与注册表
trading/                 回测、评分、交易计划、资金/情绪因子
utils/                   数据源、飞书推送、幂等台账、缓存
stock_analyzer/          深度分析与报告生成
web/                     本地管理界面（Flask）
config/                  配置与参数（strategy_*.yaml / risk_*.yaml）
cloudflare/              统一准点调度 Worker
data/                    SQLite 与运行期产物（不入库）
tests/                   CI 门禁：语法 / 单元 / 档期一致性 / webhook 扫描
.github/workflows/       定时任务与 CI
```

## 本地开发

```bash
pip install -r requirements.txt

python main.py init                        # 首次初始化数据库，30~45 分钟
python intraday_selection.py --dry-run     # 只打印卡片，不推送
python scripts/alert.py --check            # 只读校验 webhook / 加签配置
python -m unittest discover -s tests       # 全部守护测试
```

Web 管理界面：双击 `start.bat`（Windows）或 `python web_server.py`。

## 数据与配置口径

| 项 | 说明 |
| --- | --- |
| 数据库 | SQLite，路径见 `config/database.yaml`，运行期文件不入库 |
| 策略参数 | `config/strategy_params.yaml` / `strategy_weights.json` / `strategy_kelly_config.yaml` |
| 依赖版本 | akshare 钉死 `==1.20.42`：上游改字段名会静默取到空数据，升级需同步改 workflow 的 pin 并跑 `verify-valuation.yml` 实测 |
| 调度口径 | 任务按「行情时间戳里的日期」记档期，不按运行日期 |

## 安全说明

- Webhook / 加签密钥**只**走 Secrets 或环境变量；`config/config.yaml` 的 `feishu.webhook_url` 必须留空，CI 有硬门禁扫描（`tests/test_smoke.py::test_no_real_webhook_committed`）。
- 若你 fork 后复用了本仓库历史，请**重新创建机器人**再配 Secrets：Git 历史中的旧值删不掉，任何拿到 clone 的人都能读到。
- 加签要么两个机器人都开，要么都不开：只开一半会让业务推送与告警同时静默。
- 未配置 Webhook 会让任务判红，绝不允许「运行页绿色但群里没消息」。
- Worker 运维端点优先用 `X-Admin-Token` 请求头，避免 `?key=` 落进访问日志与浏览器历史。
- 所有 Action 固定到 commit SHA，并由 dependabot 每周提升级 PR。

## 排错

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| 飞书返回 19021 | 机器人开启加签但密钥未配/配错 | 补配 `FEISHU_SECRET`，或关闭加签 |
| 卡片日期与预期不符 | cron 常延迟数小时 | 任务按行情时间戳记档期，属预期行为 |
| Actions 绿但群里没消息 | `FEISHU_WEBHOOK` 未配或机器人被重置 | 重新配置 Secrets |
| 档期对账判红 | 某档确实没跑 | 看 `schedule-audit` artifact，用 workflow_dispatch 手动补档 |
| 取数为空 | akshare 上游字段变更 | 跑 `verify-valuation.yml` 定位，再决定是否升级 pin |

## 相关文档

- [完整说明与排错](docs/README-full.md)
- [Cloudflare Worker 统一调度器](cloudflare/README.md)
- [策略规格清单](strategy/strategy_specifications.md)
- [档期一致性检查](.github/workflows/schedule-audit.yml)
- [上游项目与致谢](notice.md)

## 免责声明

本工具仅用于研究与复盘，不构成投资建议。市场有风险，投资需谨慎；过往表现不预示未来收益。

## License

MIT；本项目为 [a-share-quant-selector](https://github.com/Dzy-HW-XD/a-share-quant-selector) 的衍生作品，版权与许可声明见 [LICENSE](LICENSE) 与 [notice.md](notice.md)。
