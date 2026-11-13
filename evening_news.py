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


def get_index_data():
    """获取主要指数行情"""
    lines = []
    index_codes = [
        ("sh000001", "上证指数"),
        ("sz399001", "深证成指"),
        ("sz399006", "创业板指"),
        ("sh000688", "科创50"),
    ]
    for code, name in index_codes:
        try:
            resp = requests.get(
                f"https://qt.gtimg.cn/q={code}",
                timeout=5,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            data = resp.text.split("~")
            if len(data) > 45:
                price = data[3]
                pct = data[32]
                vol = float(data[37]) / 10000
                emoji = "🔴" if float(pct) > 0 else "🟢" if float(pct) < 0 else "⚪"
                lines.append(f"  {emoji} {name}: {price} ({pct}%) {vol:.0f}亿")
        except Exception:
            pass
    return lines


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
    """获取财经要闻"""
    lines = []
    try:
        resp = requests.get(
            "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&k=&num=15&page=1",
            timeout=8,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://finance.sina.com.cn",
            },
        )
        news_list = resp.json().get("result", {}).get("data", [])
        for n in news_list[:count]:
            title = n.get("title", "")
            intro = n.get("intro", "")
            if title:
                lines.append(f"  • {title}")
                if intro:
                    lines.append(f"    {intro}")
    except Exception:
        pass
    return lines


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
