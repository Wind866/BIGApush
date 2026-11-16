#!/usr/bin/env python3
"""【放到 BIGApush 仓库】导出收盘选股结果，供 A股事件筛选器做「选股 × 事件」交叉。

读 data/stock_selection.db 的 stock_selection_record 表，写 exports/selection-latest.json。
数据库不存在 / 表不存在 / 当天无记录时**一律 exit 0**，绝不影响选股主流程。

用法（在 BIGApush 仓库根目录）：
    python scripts/export_selection.py
    python scripts/export_selection.py --days 10 --out exports/selection-latest.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
from pathlib import Path

WANTED = (
    "stock_code", "stock_name", "strategy_name", "selection_date", "selection_time",
    "selection_price", "score", "rank_position", "industry", "sector", "strategy_count",
)


def split_strategies(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[,，+、;/\s]+", text or "") if s.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/stock_selection.db")
    parser.add_argument("--out", default="exports/selection-latest.json")
    parser.add_argument("--days", type=int, default=10)
    args = parser.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"[导出] 数据库不存在（{db}），跳过")
        return 0

    out = Path(args.out)
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        columns = {row["name"] for row in con.execute("PRAGMA table_info(stock_selection_record)")}
        if not columns:
            print("[导出] 没有 stock_selection_record 表，跳过")
            return 0
    except sqlite3.Error as exc:
        print(f"[导出] 打开数据库失败：{exc}")
        return 0

    cutoff = (dt.date.today() - dt.timedelta(days=args.days)).isoformat()
    has_active = "is_active" in columns
    sql = "SELECT {cols} FROM stock_selection_record WHERE selection_date >= ?".format(
        cols=", ".join(c for c in WANTED if c in columns)
    )
    if has_active:
        sql += " AND is_active = 1"
    sql += " ORDER BY selection_date DESC, rank_position ASC"
    if "rank_position" not in columns:
        sql = sql.replace(", rank_position ASC", "")

    try:
        rows = con.execute(sql, (cutoff,)).fetchall()
    except sqlite3.Error as exc:
        print(f"[导出] 查询失败：{exc}")
        return 0
    finally:
        con.close()

    if not rows:
        print(f"[导出] {cutoff} 之后没有选股记录，跳过")
        return 0

    picks: dict[tuple[str, str], dict] = {}
    for row in rows:
        item = dict(row)
        code = str(item.get("stock_code") or "").strip()
        date = str(item.get("selection_date") or "")[:10]
        if not code or not date:
            continue

        strategies = split_strategies(str(item.get("strategy_name") or ""))
        key = (code, date)
        if key in picks:
            picks[key]["strategies"] = sorted({*picks[key]["strategies"], *strategies})
            picks[key]["strategyCount"] = max(picks[key]["strategyCount"], len(picks[key]["strategies"]))
            continue

        picks[key] = {
            "code": code.zfill(6),
            "name": str(item.get("stock_name") or "").strip(),
            "date": date,
            "selectionTime": str(item.get("selection_time") or ""),
            "strategies": strategies,
            "strategyCount": int(item.get("strategy_count") or len(strategies) or 1),
            "score": item.get("score"),
            "rank": item.get("rank_position"),
            "price": item.get("selection_price"),
            "industry": item.get("industry"),
            "sector": item.get("sector"),
        }

    payload = {
        "source": "BIGApush",
        "generatedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "asOf": max(p["date"] for p in picks.values()),
        "days": args.days,
        "count": len(picks),
        # 日期倒序；同一天内名次靠前的排前面
        "picks": sorted(picks.values(), key=lambda p: (p["date"], -(p["rank"] or 999)), reverse=True),
    }

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[导出] {len(picks)} 条选股记录 → {out}（asOf={payload['asOf']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
