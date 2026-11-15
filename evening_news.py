#!/usr/bin/env python3
"""
晚间股市新闻推送
每天 22:13（北京时间）运行，推送当日收盘总结和晚间要闻
"""
import requests
import os
import sys
from datetime import datetime, timedelta, timezone

# GitHub Actions runner 默认 UTC，显式使用北京时间，保证标题与日志时间正确
BJ_TZ = timezone(timedelta(hours=8))

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sector_data import format_sector_message
from utils import biga_sources as bs
from utils import feishu_webhook as fw
from utils.push_guard import edition_date, mark_pushed, should_skip

# 本文件对应的工作流，用于幂等检查（外部定时器与仓库 cron 会重复触发）
WORKFLOW_FILE = "evening-news.yml"
# 目标档期整点（22:13 档）。档期键按「运行时刻 − 22h」归并，所以次日凌晨甚至
# 上午 11 点才跑到的迟到运行，仍然算作**前一天**的晚间档，不会把当天的档期
# 提前标记成"已推送"而导致当晚漏推。
EDITION_SLOT_HOUR = 22
EDITION_CUTOFF_HOUR = 8

# 内容闸门：三个数据块里至少要有 2 块拿到真实数据才允许推送。
# 没有闸门时，指数/板块/新闻三源全挂也会发出一条空壳消息，随后
# mark_pushed() 把「本档期已推送」写进台账 —— 后面几档 cron 补跑会被
# 幂等守卫跳过，当天就静默漏推了。宁可让这一档判红并告警。
MIN_VALID_BLOCKS = 2


def sector_has_data(msg: str) -> bool:
    """format_sector_message() 永远返回非空文本，缺数据时写的是「暂不可用」。

    所以不能只看非空，要看有没有实质行（领涨/领跌/净流入/净流出）。
    """
    if not msg:
        return False
    return any(tag in msg for tag in ("领涨:", "领跌:", "净流入:", "净流出:"))


def content_quality(idx_lines, sector_msg, news) -> tuple[bool, dict]:
    """返回 (是否达到推送门槛, 各数据块可用性)。"""
    blocks = {
        "指数行情": bool(idx_lines),
        "板块数据": sector_has_data(sector_msg),
        "财经要闻": bool(news),
    }
    return sum(blocks.values()) >= MIN_VALID_BLOCKS, blocks


def get_index_data():
    """多源指数行情：腾讯批量 → 新浪 → 东财多域名（见 utils/biga_sources.py）。"""
    try:
        lines, label = bs.fetch_index_lines()
        print(f"[数据源] 指数行情：{label}")
        return lines
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 全部指数数据源不可用：{type(exc).__name__} {exc}")
        return []


def get_sector_data():
    """获取板块涨跌"""
    try:
        import re, json
        resp = requests.get(
            "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php",
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        match = re.search(r"=\s*(\{.*\})", resp.text, re.DOTALL)
        if match:
            data = json.loads(match.group(1))
            sectors = []
            for v in data.values():
                parts = v.split(",")
                if len(parts) > 5:
                    sectors.append((parts[1], float(parts[4]) if parts[4] else 0))
            sectors.sort(key=lambda x: x[1], reverse=True)
            up = [f"{n}({p:+.2f}%)" for n, p in sectors[:5]]
            dn = [f"{n}({p:+.2f}%)" for n, p in sectors[-5:]]
            return up, dn
    except Exception:
        pass
    return [], []


def get_news(count=10):
    """多源财经要闻：新浪滚动 → 同花顺快讯 → 新浪 RSS → 新浪备用栏目。"""
    try:
        lines, label = bs.fetch_news(count)
        print(f"[数据源] 财经要闻：{label}")
        return lines
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 全部新闻数据源不可用：{type(exc).__name__} {exc}")
        return []


def send_feishu(msg):
    """发送飞书消息；支持加签、网络重试，且校验飞书业务码。

    未配置 webhook 一律返回 False —— 绝不返回 True，否则上层会把它记成
    「推送成功」写进台账，后续补跑被幂等守卫跳过。
    """
    hook, secret = fw.resolve_credentials()
    if not hook:
        print("::warning::FEISHU_WEBHOOK 未配置，消息已生成但不会推送，请在仓库 Secrets 中配置")
        print("（消息预览）" + msg[:200].replace("\n", " / "))
        return False
    try:
        result = fw.send_text(hook, secret, msg)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 飞书推送异常：{type(exc).__name__} {exc}")
        return False
    if not fw.response_ok(result):
        print(f"❌ 飞书返回错误：{result}")
        return False
    return True


def main():
    now = datetime.now(BJ_TZ)
    lines = []
    # --dry-run：只生成消息并打印，不推送、不写台账。
    # 没有它的话，本地想看一眼排版就必须先配一个真 webhook，否则脚本在取数前就退出了——
    # README 里「未配置 webhook 也能验证内容」的描述与旧实现并不一致。
    dry_run = "--dry-run" in sys.argv

    # 未配置 webhook 时直接跳过，避免日志出现「推送成功」造成误导
    if not os.environ.get("FEISHU_WEBHOOK", "").strip() and not dry_run:
        msg = "FEISHU_WEBHOOK 未配置，跳过本次推送（请在仓库 Secrets 中配置）"
        if os.environ.get("CI"):
            print(f"::error::{msg}")
            sys.exit(1)
        print(f"::warning::{msg}")
        return

    # 幂等守卫：Cloudflare Worker 准点触发 + GitHub cron 延迟触发会打两次，
    # 同一档期只让先到的那次真的推送（FORCE_PUSH=1 或 --force 可强制重推）
    force = "--force" in sys.argv or os.environ.get("FORCE_PUSH", "").strip() in ("1", "true", "yes")
    if not dry_run and should_skip(WORKFLOW_FILE, force=force, slot_hour=EDITION_SLOT_HOUR):
        return

    # 标题用「档期日期」而非运行日期。本账号 cron 实测延迟 4h57m~7h18m，
    # 22:13 档常在次日凌晨 03:00~05:30 才跑；用运行日会把前一天的档期标成新的一天。
    edition = edition_date(now, EDITION_CUTOFF_HOUR, EDITION_SLOT_HOUR).isoformat()
    lines.append(f"🌙 晚间缅A · {edition} 档期")
    lines.append(f"数据时点：{now:%Y-%m-%d %H:%M}（北京时间）")
    lines.append("")

    # 调度延迟标注：cron 晚几小时是常态，必须写清楚内容对应哪个时点
    expect = os.environ.get("EXPECT_HHMM", "").strip()
    if len(expect) == 4 and expect.isdigit():
        planned = now.replace(hour=int(expect[:2]), minute=int(expect[2:]),
                              second=0, microsecond=0)
        late = int((now - planned).total_seconds() // 60)
        if late >= 60:
            lines.append(f"⚠️ 本档期延迟 {late} 分钟（GitHub 调度），"
                         f"下为 {now:%m-%d %H:%M} 时点数据")
            lines.append("")

    # 今日大盘收盘：延迟到次日时标题不带「今日」，避免口径错行
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("📈 收盘行情（时点见上）")
    lines.append("")
    idx_lines = get_index_data()
    if idx_lines:
        lines.extend(idx_lines)
        lines.append("")
    else:
        lines.append("  ⚠️ 指数行情暂不可用")
        lines.append("")

    # 板块数据（行业+概念+资金流向）
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    try:
        sector_msg = format_sector_message()
    except Exception as exc:  # noqa: BLE001 - 单块失败不该让整档崩掉
        print(f"⚠️ 板块数据组装失败：{type(exc).__name__} {exc}")
        sector_msg = ""
    lines.append(sector_msg or "  ⚠️ 板块数据暂不可用")
    lines.append("")

    # 晚间要闻
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("📰 晚间财经要闻（取数时点见上）")
    lines.append("")
    news = get_news(10)
    if news:
        lines.extend(news)
    else:
        lines.append("  ⚠️ 财经要闻暂不可用")

    # 内容闸门：不合格直接判红，让后续 cron 档期还能补跑。
    ok, blocks = content_quality(idx_lines, sector_msg, news)
    print(f"[内容闸门] {blocks} → {'通过' if ok else '不通过'}"
          f"（要求 ≥{MIN_VALID_BLOCKS} 块有真实数据）")
    if not ok and not dry_run and not force:
        print("::error::数据源大面积失效，消息内容不完整，本次不推送也不写台账；"
              "后续兜底档期会重试（应急可用 FORCE_PUSH=1 强制推送）")
        sys.exit(1)

    # 发送
    msg = "\n".join(lines)
    if dry_run:
        print("---- 消息预览（--dry-run，未推送）----")
        print(msg)
        print("------------------------------------")
        return
    if send_feishu(msg):
        print(f"✅ 晚间新闻推送成功 ({now.strftime('%H:%M:%S')} 北京时间)")
        # 只有飞书确实推送成功才记档期；非交易日/调试/失败的运行不会污染台账
        mark_pushed(WORKFLOW_FILE, slot_hour=EDITION_SLOT_HOUR)
    else:
        print("❌ 推送失败（未写台账，兜底档期可重试）")
        sys.exit(1)


if __name__ == "__main__":
    main()
