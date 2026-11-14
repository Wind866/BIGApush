# 缅A每日推送

A股量化选股 + 每日自动推送系统，通过 GitHub Actions 全自动运行，结果推送到飞书群。

> ## ⚠️ 安全提醒（请务必先看）
>
> 本仓库**曾经把真实飞书机器人 Webhook 明文提交进 `config/config.yaml`**。公开仓库会被爬虫抓取，
> 任何拿到该 Webhook 的人都能向你的群发消息。当前文件已清空，但 **Git 历史里仍然存在**。
>
> **必须做一次**：飞书群 → 设置 → 群机器人 → 删除并重建该自定义机器人 → 把新 Webhook 填进
> GitHub 仓库 `Settings → Secrets and variables → Actions → FEISHU_WEBHOOK`。
> 不要把 Webhook 写进任何会被提交的文件（包括 `config/config.yaml`）。

## 功能特性

- **收盘选股**：15 种量化选股策略，覆盖底部反转、趋势加速、形态突破
- **盘中选股**：13:00 基于实时量价与主力资金的强势股观察池（与收盘选股口径不同，见下）
- **大盘复盘 / 板块分析 / 资金流向 / 财经要闻**
- **新增去除对比**、**买入建议评分**（均线 / RSI / MACD / 量能）
- **精确交易日判断**：用行情时间戳比对北京日期，可识别法定节假日，非交易日自动跳过
- **飞书推送**：盘中为交互卡片（失败自动降级文本），收盘与新闻为文本消息
- **失败可见**：推送失败或任务异常会让 Actions 变红并推送告警，不再「假绿」

## 自动运行时间（北京时间）

| 时间 | 任务 | Workflow | 内容 |
|------|------|----------|------|
| **08:30**（每天） | 早间新闻 | `morning-news.yml` | 上个交易日大盘回顾 + 板块 + 财经要闻 |
| **13:00**（交易日） | **盘中选股** | `intraday-selection.yml` | 实时强势观察池 + 主力资金 + 板块共振 |
| **15:30**（交易日） | 收盘选股 | `selection.yml` | 15 种策略选股 + 复盘 + 板块 + 新闻 + 买入建议 |
| **22:00**（每天） | 晚间新闻 | `evening-news.yml` | 收盘行情 + 板块 + 晚间要闻 |
| 周二 01:00（每周） | 保持仓库活跃 | `keep-alive.yml` | 心跳提交，防止 GitHub 60 天后禁用定时任务 |

> GitHub Actions 的 cron 使用 **UTC**，换算规则：`UTC = 北京时间 − 8 小时`。
> 例如 13:00 → `0 5 * * 1-5`，15:30 → `30 7 * * 1-5`，08:30 → `30 0 * * *`，22:00 → `0 14 * * *`。
> 定时任务通常有 **5~15 分钟延迟**，属正常现象。改时间只需编辑对应 workflow 的 `schedule` 行后 push。

所有 workflow 都设置了 `TZ: Asia/Shanghai`。这一点很关键：runner 默认 UTC，
早期版本用 `datetime.now()` 判断「是否收盘」，导致北京时间 15:10 被认为是 07:10
而**每天都跳过数据更新**，选股一直基于首次初始化的旧数据。现已修复。

## 盘中选股（13:00）说明

`intraday_selection.py` 是**独立的轻量脚本**，与主程序完全解耦：

- 只用 Python 标准库，不装 akshare、不读数据库、不做首次全量初始化，Actions 上约 10 秒跑完；
- 从东方财富拉当日实时全市场快照，按涨幅降序分页扫描强势股（通常 3~8 页即可覆盖全部涨幅 ≥3% 的标的）；
- 筛选条件：排除 ST / 退市 / 北交所，涨幅 ≥3%、主力资金净流入、成交额 ≥5000 万、
  流通市值 15~1500 亿、换手 ≥1%；
- 七因子加权评分：**涨幅动量 20% / 主力净流入 28% / 超大单 14% / 量比 12% /
  换手 8% / 板块共振 10% / 流通市值 8%**，再对已封板、高换手、异常放量扣分；
- 推送 Top10 + 板块共振，飞书卡片明确标注「盘中快照」，并附「查看本次运行日志」按钮。

**数据源容灾**：主源为东方财富实时行情（含量价、主力资金、超大单、行业）。
该接口偶发限流时，脚本会自动切换到新浪财经全市场涨幅榜，改用「纯量价」权重
（涨幅 32% / 成交活跃度 25% / 换手 15% / 流通市值 28%，资金与板块权重归零），
并在卡片里明确标注「本期不含主力资金与板块共振」，
不会因为一个接口抖动就收不到 13:00 的卡片。
分页失败时也会用已获取的数据继续，只有两个数据源都不可用才让任务失败并告警。

**与收盘选股的区别**：收盘选股是「已收盘 K 线 + 15 种形态策略」的定稿信号；
盘中选股是「实时量价 + 资金流」的强势股跟踪，午后可能变盘。
盘中脚本**不写入主程序数据库**，因此不会污染收盘选股的「与上一交易日对比」和历史记录。

本地试跑：

```bash
python intraday_selection.py --dry-run            # 真实行情，只打印卡片不推送
python intraday_selection.py --check-trading-day  # 仅判断今天是否交易日
FEISHU_WEBHOOK=https://... python intraday_selection.py   # 实推一次
```

## 15 种收盘选股策略

| # | 策略名称 | 核心逻辑 | 适用场景 |
|----|---------|--------|--------|
| 1 | **底部趋势拐点** | 深度下跌后的反转拐点 | 极端底部 |
| 2 | **涨停回马枪策略** | 涨停后回调再次启动 | 短期强势 |
| 3 | **涨停横盘策略** | 涨停后横盘整理突破 | 突破选股 |
| 4 | **启明星策略** | 三根K线底部反转形态 | 底部反转 |
| 5 | **多金叉共振** | 均线/KDJ/MACD金叉共振 | 多头共振 |
| 6 | **多方炮策略** | 两阳夹一阴K线组合 | 短期反弹 |
| 7 | **阻力位突破策略** | 股价突破关键阻力位 | 突破选股 |
| 8 | **强势洗盘弱转强** | 强势股洗盘后转强 | 趋势反转 |
| 9 | **趋势加速拐点** | 上升趋势中的加速拐点 | 趋势加速 |
| 10 | **仙人指路策略** | 长上影线突破形态 | 突破选股 |
| 11 | **W底策略** | W底双底反转形态 | 双底反转 |
| 12 | **趋势起点策略** | 趋势启动初期识别 | 趋势启动 |
| 13 | **2560战法** | 基于特定K线形态的选股策略 | 形态突破 |
| 14 | **金三角策略** | A/B/C三点金叉形态检测趋势拐点 | 趋势拐点 |
| 15 | **金叉不绿策略** | 金叉形态 + 绿色K线过滤提升信号质量 | 精准金叉 |

## 买入建议评分标准

| 评分 | 建议 | 说明 |
|------|------|------|
| 🟢 70分以上 | 建议买入 | 技术面多项指标向好 |
| 🟡 55-69分 | 可以关注 | 部分指标良好 |
| ⚪ 40-54分 | 建议观望 | 指标中性 |
| 🔴 40分以下 | 建议回避 | 技术面走弱 |

分析维度：均线排列、RSI指标、MACD趋势、成交量变化、涨跌幅

## 快速开始

1. **Fork 仓库**（或 Push 到自己的空仓库）。若用网页拖拽上传，务必包含隐藏目录 `.github/workflows/`，漏传就没有任何定时任务。
2. **配置飞书 Webhook**：群设置 → 群机器人 → 添加自定义机器人 → 复制 Webhook →
   仓库 `Settings → Secrets and variables → Actions` 新建 Secret **`FEISHU_WEBHOOK`**。
3. **启用 Actions**：仓库 Actions 页点 "Enable workflow"。
4. **手动测试**：Actions → 「盘中选股」→ Run workflow。想快速验证推送链路，
   可跑「收盘选股」并勾选 `skip_update`（跳过耗时的全量数据更新，用缓存数据库）。
5. 手动测试通过后再等定时触发。GitHub 要求仓库 60 天内有活动，已内置每周心跳 workflow。

> **未配置 `FEISHU_WEBHOOK` 时**：消息会正常生成但不推送，Actions 日志显示 `::warning::`，任务仍为绿色。
> 配置后推送失败会直接让任务变红，并尝试发一条失败告警。

## 配置说明

Webhook 只通过环境变量 / Secrets 提供，`config/config.yaml` 中的 `feishu.webhook_url` 保持为空：

```yaml
data_dir: data

feishu:
  webhook_url: ""        # 留空；程序优先读环境变量 FEISHU_WEBHOOK

update:
  lookback_days: 10
  skip_failed: true

filters:
  min_market_cap: 40     # 最小市值（亿）
  max_market_cap: 5000   # 最大市值（亿）
  exclude_st: true       # 排除 ST
```

`config/config.yaml.template` 是可复制的模板。其它策略参数在 `config/strategy_*.yaml` 中调整。

**关于收盘选股的数据更新**：修复时区后，`main.py` 会在 15:30 真正执行每日增量更新
（此前因 UTC 误判一直被跳过）。首次运行需 `python main.py init` 全量初始化，约 30~45 分钟；
之后每日增量更新耗时取决于数据库规模，workflow 已放宽到 90 分钟。
若遇到超时或想快速验证推送，可在手动触发时勾选 `skip_update`（设置 `SKIP_DATA_UPDATE=1`）。
注意 GitHub 免费账号每月 Actions 额度 2000 分钟，全量更新较耗时，请按需取舍。

## 本地运行

```bash
pip install -r requirements.txt

python main.py init                      # 首次全量初始化
python main.py run                       # 收盘选股并推送
python intraday_selection.py --dry-run   # 盘中选股（不推送）
python daily_news.py                     # 早间新闻
python evening_news.py                   # 晚间新闻
python check_sources.py                  # 数据源健康自检（纯标准库）
python check_sources.py --markdown       # 生成可贴进 CI Summary 的体检报告
python run_scheduled.py --list           # 本地定时兜底（Actions 不可用时）
python main.py web                       # 启动 Web 界面
```

## 数据源与容灾

所有实时取数统一走 `utils/biga_sources.py` + `utils/market_sources.py`
（与 [limit-up-relay](https://github.com/Wind866/limit-up-relay) 同源，纯标准库、
零新增依赖）。单家供应商被限流、改字段或下线时自动切到下一层，不会再让整条
流水线只剩「空消息」或历史假数据。

| 链路 | 主源 | 备用源（按顺序） |
|------|------|------------------|
| 盘中强势股池 | 东财 `clist` 多域名（含主力/超大单/行业/量比） | 新浪全市场涨幅榜 → 同花顺涨停池 |
| 备用池字段补齐 | 东财 `ulist.np` 多域名（可恢复完整同口径） | 腾讯行情（量比/换手/流通市值）→ 新浪资金流（口径不同） |
| 交易日判断 | 东财指数时间戳（多域名） | 腾讯指数行情时间戳 |
| 早/晚新闻要闻 | 新浪滚动 2516 | 同花顺快讯 → 新浪 2515 → 新浪 2517 → 同花顺次页 |
| 大盘指数 | 腾讯批量行情 | 新浪指数 → 东财 `ulist.np` 多域名 |
| 行业/概念板块 | 新浪行业 / 概念 | 东财板块榜（多域名） |
| 收盘选股实时行情 | 腾讯 `qt.gtimg.cn` | `sqt.gtimg.cn` → `http://qt.gtimg.cn`（同口径三域名） |
| 历史 K 线 | AKShare / 东财 / TickFlow | 腾讯 K 线（原有链路保持不变） |

同供应商内的冗余：东财 `push2` 有 `push2 / 82.push2 / push2delay` 三个域名，
`push2ex` 另有 http 降级；实测某一域名被限流时换域名即可恢复。

**质量分级与口径红线**

| 质量 | 含义 | 评分处理 |
|------|------|----------|
| `full` | 东财完整口径，字段齐全 | 七因子含主力资金、超大单、板块共振，可与历史比较 |
| `partial` | 备用源补齐部分字段 | 量价口径；行业字段不可用时跳过板块共振 |
| `minimal` | 仅量价 | 量价口径，分数仅用于当日内部排序 |

- 任何缺失字段一律为 `None`，**绝不用 0 或猜测值冒充真实行情**；
- 只有东财口径的资金参与评分。备用源（新浪）补到的资金数据**仅作展示参考**，
  卡片会标注「口径不等价、未计入评分、不可与历史比较」；
- 主力资金是按成交单笔金额推断的统计量，不是交易所席位真实资金。

**健康自检与故障演练**

```bash
python check_sources.py                 # 逐项探测 18 个接口，按链路汇总
python check_sources.py --markdown      # Markdown 报告（CI Step Summary 用）
BIGA_SOURCE_EASTMONEY=0 python intraday_selection.py --dry-run   # 模拟东财全挂
BIGA_SOURCE_EASTMONEY=0 BIGA_SOURCE_SINA=0 python check_sources.py  # 多级演练
```

开关在 `config/multi_source.json`（注意：`config/data_sources.json` 是原仓库
`data_fetcher.py` 的供应商优先级配置，两者互不干扰）；环境变量
`BIGA_SOURCE_EASTMONEY / SINA / THS / TENCENT = 0` 可临时覆盖。
每周一 09:00 的「数据源健康自检」工作流会把报告写进 Run Summary。

## 项目结构

```
缅A每日推送/
├── main.py                  # 主程序入口（收盘选股）
├── intraday_selection.py    # 盘中选股（独立、纯标准库）
├── daily_news.py            # 早间新闻
├── evening_news.py          # 晚间新闻
├── check_sources.py         # 数据源健康自检（只读、纯标准库）
├── run_scheduled.py         # 本地定时兜底（不依赖 GitHub Actions）
├── simple_analyzer.py       # 买入建议分析
├── sector_data.py           # 板块数据获取（新浪 → 东财降级）
├── web_server.py            # Web 服务器
├── config/                  # 配置文件（含 multi_source.json）
├── strategy/                # 15 种选股策略
├── trading/                 # 交易 / 回测相关
├── utils/                   # 工具函数
│   ├── biga_sources.py      # 多源接入层（本项目链路映射）
│   └── market_sources.py    # 多源容灾底座（与 limit-up-relay 同源）
├── web/                     # Web 前端
├── data/                    # 数据文件（db 不上传）
└── .github/workflows/       # 10 条 GitHub Actions 流水线
```

## GitHub Actions 流水线

| Workflow | 说明 | 触发时间（北京时间） |
|----------|------|----------|
| `intraday-selection.yml` | 盘中选股观察池 | 交易日 13:00 |
| `selection.yml` | 收盘选股 + 复盘 | 交易日 15:30 |
| `morning-news.yml` | 早间新闻 | 每天 08:30 |
| `evening-news.yml` | 晚间新闻 | 每天 22:00 |
| `keep-alive.yml` | 保持仓库活跃 | 每周二 01:00 |
| `check-sources.yml` | 数据源健康自检（只读） | 每周一 09:00，也可手动运行 |
| `stock-query.yml` | 个股深度分析（查任意股 → 策略信号 + 买入建议 → 推飞书图片卡片：K 线 MA5/10/20/60 + 16 项指标 + 筹码分布 + 结论） | 仅手动 |
| `verify-valuation.yml` | 真实数据取自检（估值/资金流/行情/指标，只打日志不推送） | 仅手动 |

所有流水线都配置了最小权限 `permissions: contents: read`、
`concurrency` 防重复运行、`TZ: Asia/Shanghai` 与失败告警。
选股类任务在装依赖前先用纯标准库脚本做交易日预检，节假日直接跳过，
不会白白消耗 Actions 额度或推送空消息。

## GitHub Actions 被停用时的本地兜底

如果仓库页面出现红字 **"GitHub Actions is currently disabled for this
repository. Please reach out to GitHub Support for assistance."**，说明是
GitHub 平台侧的禁用（常见于风控/滥用检测或额度限制），**不是 workflow 写错，
也无法在 Settings → Actions 里自行重新打开**。可参考下面两条路径：

1. 打开 `Settings → Actions → General` 与 `Settings → Billing and plans` 确认，
   检查账号绑定邮箱有无 GitHub 通知，然后到 <https://support.github.com/request>
   提交工单申请复核（附仓库地址与完整报错）。社区反馈这类限制有时无法解除。
2. 若短期无法恢复，把定时任务移到本机/服务器，`run_scheduled.py` 会按同一张
   时间表分发，飞书 Webhook 仍从环境变量 `FEISHU_WEBHOOK` 读取：

```bash
# 一次性确认时间表 / 当前状态
python run_scheduled.py --list
python run_scheduled.py --status

# Linux：每 10 分钟调用一次（脚本自己判断该不该跑、同一时点当天只跑一次）
*/10 * * * * cd /path/to/BIGApush && /usr/bin/python3 run_scheduled.py >> data/scheduler.log 2>&1
```

Windows 用「任务计划程序」新建任务：程序 `python.exe`、参数 `run_scheduled.py`、
起始位置填仓库根目录，触发器设为每天每 10 分钟重复。
也可以把代码推到一个新建的仓库，在 Actions 恢复正常后重新启用 6 条流水线。

> 注：`keep-alive` 的自动提交是常见的风控触发点之一。若准备申诉，建议先停用
> 该工作流（Settings → Actions → 对应 workflow → Disable），或删除后改用本地兜底。

## 常见问题

| 现象 | 处理 |
|------|------|
| Actions 绿色但飞书没消息 | 检查 Secret `FEISHU_WEBHOOK` 是否配置、机器人是否被移除或重置；日志会打印 `::warning::` |
| 收盘选股运行很久 / 超时 | 首次全量初始化较慢；手动触发可勾选 `skip_update`，或检查 `data/stock_selection.db` 缓存是否命中 |
| 想改推送时间 | 编辑对应 workflow 的 `cron`，记得 `UTC = 北京时间 − 8 小时`，改完 push 即生效 |
| 节假日仍收到推送 | 交易日预检依赖东方财富行情时间戳；接口临时不可用时按周末规则放行，可手动禁用当天任务 |
| 选股结果几天不变 | 旧版本 UTC 时区 bug 导致跳过数据更新；更新到本版本后按 15:30 正常增量更新 |
| 手动重跑旧失败记录 | 请点 **Run workflow** 而不是失败运行里的 Re-run，后者会沿用旧提交时的 workflow 定义 |
| Actions 页显示 `GitHub Actions is currently disabled for this repository` | 平台侧禁用，不是配置错误，需联系 GitHub Support 复核；见上一节的本地兜底方案 |
| 卡片出现「量价口径 / 资金仅作参考」 | 东财主接口被限流，容灾链已自动切到新浪/腾讯/同花顺；可跑 `python check_sources.py` 查看具体哪个源挂了 |
| 降级当天的分数和历史对不上 | 设计如此：备用源资金口径与东财不等价，不参与评分，分数只用于当日内部排序 |

## 参考项目

本项目基于以下开源项目开发：

- [KHunter](https://github.com/ling-0729/KHunter) - A股量化交易系统，提供选股策略和数据分析框架

## 免责声明

本工具仅用于学习和研究，基于公开数据与规则化评分，**不构成任何投资建议**。
主力资金为按成交单笔金额推断的统计量，并非交易所席位真实资金；盘中快照午后可能变盘，
与收盘信号不等价；策略未经样本外寻优，历史表现不预示未来收益。
股市有风险，投资需谨慎，任何决策请结合自身风险承受能力独立判断，必要时咨询持牌专业机构。

## License

MIT
