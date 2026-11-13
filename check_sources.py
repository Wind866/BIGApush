#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据源健康自检：逐个探测所有已接入接口，确认哪个源挂了、该切到哪一层。

用法：
    python check_sources.py                     # 人类可读表格
    python check_sources.py --markdown         # 写入 GitHub Step Summary
    python check_sources.py --json             # 机器可读 JSON
    python check_sources.py --json --out out/sources.json

退出码：任一链路（分组）的全部备用源都失效 → 1（真故障，需要人工处理）；
        部分接口失效但容灾链仍可工作 → 0。

自检只读接口，不写数据库、不推飞书、不产生任何交易信号，纯标准库无需 pip。
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from utils import biga_sources as bs  # noqa: E402
from utils import market_sources as s  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

GROUP_LABELS = {
    "snapshot": "盘中选股 · 强势股池与行情",
    "flow": "资金拆解",
    "calendar": "交易日判断 · 指数时间戳",
    "news": "早/晚新闻 · 要闻与大盘",
    "board": "板块行情",
}


def run() -> dict:
    bs.configure()
    s.HEALTH.reset()
    results = []
    for group, provider, label, endpoint, fn in bs.probes():
        t0 = time.perf_counter()
        try:
            detail = str(fn())
            ok = True
        except Exception as exc:  # noqa: BLE001 - 自检要如实记录每种失败
            detail = f"{type(exc).__name__}: {str(exc)[:130]}"
            ok = False
        results.append({
            "group": group, "provider": provider, "label": label,
            "endpoint": endpoint, "ok": ok,
            "ms": int((time.perf_counter() - t0) * 1000), "detail": detail,
        })

    groups: dict[str, dict] = {}
    for r in results:
        g = groups.setdefault(r["group"], {"ok": 0, "fail": 0, "total": 0})
        g["total"] += 1
        g["ok" if r["ok"] else "fail"] += 1
    broken = [g for g, v in groups.items() if v["ok"] == 0]
    ok_count = sum(1 for r in results if r["ok"])
    return {
        "checked_at": dt.datetime.now(s.CN_TZ).strftime("%Y-%m-%d %H:%M:%S %z"),
        "total": len(results), "ok": ok_count, "failed": len(results) - ok_count,
        "groups": groups, "broken_groups": broken,
        "healthy": not broken,
        "results": results,
    }


def to_markdown(result: dict) -> str:
    lines = [
        "## 🔌 数据源健康自检",
        "",
        f"检查时间：{result['checked_at']} · "
        f"可用 **{result['ok']}/{result['total']}** 个接口 · "
        f"状态：{'✅ 所有链路均有可用源' if result['healthy'] else '❌ 存在整条链路失效'}",
        "",
        "| 链路 | 可用/探测 | 状态 |",
        "| --- | --- | --- |",
    ]
    for g, v in result["groups"].items():
        state = "✅" if v["ok"] else "❌ 全部失效"
        lines.append(f"| {GROUP_LABELS.get(g, g)} | {v['ok']}/{v['total']} | {state} |")
    lines += ["", "| 供应商 | 接口 | 结果 | 耗时 | 说明 |",
              "| --- | --- | --- | --- | --- |"]
    for r in result["results"]:
        state = "✅" if r["ok"] else "❌"
        detail = r["detail"].replace("|", "\\|")
        lines.append(f"| {r['label']} | `{r['endpoint']}` | {state} | "
                     f"{r['ms']}ms | {detail} |")
    lines += [
        "",
        "> 部分接口失败属正常现象：容灾链会自动切换到下一供应商。"
        "只有某个链路的全部源都不可用时才会影响出数，此时本检查以退出码 1 告警。",
        "",
        "> 本地临时停用某个源做演练：`BIGA_SOURCE_EASTMONEY=0 python check_sources.py`",
        "",
    ]
    return "\n".join(lines)


def to_table(result: dict) -> str:
    lines = [f"数据源健康自检 · {result['checked_at']} · "
             f"可用 {result['ok']}/{result['total']}", ""]
    for r in result["results"]:
        mark = "OK  " if r["ok"] else "FAIL"
        lines.append(f"[{mark}] {r['group']:9s} {r['label']:28s} "
                     f"{r['ms']:5d}ms  {r['detail'][:90]}")
    lines.append("")
    for g, v in result["groups"].items():
        lines.append(f"  {GROUP_LABELS.get(g, g):28s} {v['ok']}/{v['total']} 可用")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="BIGApush 数据源健康自检")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--markdown", action="store_true", help="输出 Markdown 表格")
    ap.add_argument("--out", help="同时把结果写入指定文件（JSON）")
    args = ap.parse_args()

    result = run()
    if args.json:
        out = json.dumps(result, ensure_ascii=False, indent=2)
    elif args.markdown:
        out = to_markdown(result)
    else:
        out = to_table(result)
    print(out)

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with io.open(args.out, "w", encoding="utf-8") as f:
            f.write(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"结果已写入 {args.out}")
    return 0 if result["healthy"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
