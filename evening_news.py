#!/usr/bin/env python3
"""
晚间股市新闻推送
每天 22:00（北京时间）运行，推送当日收盘总结和晚间要闻
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
    """发送飞书消息；网络异常与飞书返回错误必须显式失败，避免 Actions 假绿。"""
    webhook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if not webhook:
        print("::warning::FEISHU_WEBHOOK 未配置，消息已生成但不会推送，请在仓库 Secrets 中配置")
        print("（消息预览）" + msg[:200].replace("\n", " / "))
        return True
    try:
        resp = requests.post(
            webhook,
            json={"msg_type": "text", "content": {"text": msg}},
            timeout=10,
        )
        resp.raise_for_status()
        result = resp.json()
        code = result.get("code", result.get("StatusCode"))
        if code not in (0, None):
            print(f"❌ 飞书返回错误：{result}")
            return False
        return True
    except Exception as exc:
        print(f"❌ 飞书推送异常：{type(exc).__name__} {exc}")
        return False


def main():
    now = datetime.now(BJ_TZ)
    lines = []

    # 未配置 webhook 时直接跳过，避免日志出现「推送成功」造成误导
    if not os.environ.get("FEISHU_WEBHOOK", "").strip():
        print("::warning::FEISHU_WEBHOOK 未配置，跳过本次推送（请在仓库 Secrets 中配置）")
        return

    # 标题
    lines.append(f"🌙 晚间缅A ({now.strftime('%Y-%m-%d')})")
    lines.append("")

    # 今日大盘收盘
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("📈 今日收盘行情")
    lines.append("")
    idx_lines = get_index_data()
    if idx_lines:
        lines.extend(idx_lines)
        lines.append("")

    # 板块数据（行业+概念+资金流向）
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    sector_msg = format_sector_message()
    if sector_msg:
        lines.append(sector_msg)
    else:
        lines.append("板块数据暂不可用")
        lines.append("")

    # 晚间要闻
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("📰 晚间财经要闻")
    lines.append("")
    news = get_news(10)
    if news:
        lines.extend(news)
    else:
        lines.append("  暂无新闻")

    # 发送
    msg = "\n".join(lines)
    if send_feishu(msg):
        print(f"✅ 晚间新闻推送成功 ({now.strftime('%H:%M:%S')} 北京时间)")
    else:
        print("❌ 推送失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
