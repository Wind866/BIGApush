#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""不依赖 GitHub Actions 的本地定时兜底入口。

适用情形：仓库被平台禁用 Actions、账号额度耗尽、或需要在内网/本机运行。
用系统调度器（Windows 任务计划程序 / Linux crontab）每 5~10 分钟调用一次
本脚本，它会按北京时间在对应时点执行任务，并用状态文件保证同一时点当天
只跑一次。

    python run_scheduled.py --list       # 查看时间表
    python run_scheduled.py --status     # 查看今日已执行任务
    python run_scheduled.py --dry-run    # 只看将要执行什么
    python run_scheduled.py              # 正常调度（配合系统定时调用）
    python run_scheduled.py --force intraday   # 立即重跑某个任务

Linux crontab（每 10 分钟）：
    */10 * * * * cd /path/to/BIGApush && /usr/bin/python3 run_scheduled.py >> data/scheduler.log 2>&1

Windows 任务计划程序：
    操作 = 启动程序，程序 = python.exe，
    参数 = run_scheduled.py，起始位置 = 仓库根目录，
    触发器 = 每天重复间隔 10 分钟。

飞书 Webhook 从环境变量 FEISHU_WEBHOOK 读取（与 GitHub Secrets 同名）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass

BJ_TZ = dt.timezone(dt.timedelta(hours=8))
ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "data", ".scheduler_state.json")
WINDOW_MINUTES = 25   # 时点后的触发窗口，容忍系统调度延迟


@dataclass(frozen=True)
class Task:
    key: str
    hhmm: str
    label: str
    args: tuple[str, ...]
    trading_day_only: bool


SCHEDULE = (
    Task("morning_news", "08:30", "早间新闻", ("daily_news.py",), False),
    Task("intraday", "13:00", "盘中选股观察池", ("intraday_selection.py",), True),
    Task("close", "15:30", "收盘选股", ("main.py", "run"), True),
    Task("evening_news", "22:00", "晚间新闻", ("evening_news.py",), False),
)


def _load_state() -> dict:
    try:
        with io.open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    # 只保留最近 7 天，避免状态文件无限增长
    keys = sorted(state.keys())[-7:]
    with io.open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({k: state[k] for k in keys}, f, ensure_ascii=False, indent=2)


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _in_window(now: dt.datetime, task: Task) -> bool:
    cur = now.hour * 60 + now.minute
    delta = cur - _minutes(task.hhmm)
    return 0 <= delta < WINDOW_MINUTES


def _run(task: Task, dry_run: bool = False) -> int:
    cmd = [sys.executable, *task.args]
    print(f"→ {now_text()} {task.label}：{' '.join(cmd)}")
    if dry_run:
        return 0
    env = os.environ.copy()
    env["TZ"] = "Asia/Shanghai"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        r = subprocess.run(cmd, cwd=ROOT, env=env, check=False)
        print(f"  退出码 {r.returncode}")
        return r.returncode
    except OSError as exc:
        print(f"  ❌ 无法启动：{exc}")
        return 1


def now_text() -> str:
    return dt.datetime.now(BJ_TZ).strftime("%H:%M:%S")


def main() -> int:
    ap = argparse.ArgumentParser(description="BIGApush 本地定时兜底调度")
    ap.add_argument("--list", action="store_true", help="列出时间表")
    ap.add_argument("--status", action="store_true", help="显示今日已执行任务")
    ap.add_argument("--dry-run", action="store_true", help="只打印不执行")
    ap.add_argument("--force", metavar="KEY", help="立即执行指定任务（忽略时间与状态）")
    args = ap.parse_args()

    if args.list:
        for t in SCHEDULE:
            print(f"{t.hhmm}  {t.key:14s} {t.label:12s} "
                  f"{'仅交易日' if t.trading_day_only else '每天'}")
        return 0

    now = dt.datetime.now(BJ_TZ)
    today = now.date().isoformat()
    state = _load_state()
    done = set(state.get(today, []))

    if args.status:
        print(f"{today} 已执行：{', '.join(sorted(done)) or '无'}")
        return 0

    if args.force:
        task = next((t for t in SCHEDULE if t.key == args.force), None)
        if not task:
            print(f"❌ 未知任务 {args.force}；可选：{', '.join(t.key for t in SCHEDULE)}")
            return 2
        return 0 if _run(task, args.dry_run) == 0 else 1

    pending = [
        t for t in SCHEDULE
        if _in_window(now, t)
        and t.key not in done
        and not (t.trading_day_only and now.weekday() >= 5)
    ]
    if not pending:
        print(f"{now_text()} 无待执行任务（今日已完成：{', '.join(sorted(done)) or '无'}）")
        return 0

    failed = 0
    for task in pending:
        # 先落状态再执行，避免任务耗时较长时被下一次调度重复触发
        done.add(task.key)
        state[today] = sorted(done)
        _save_state(state)
        failed += _run(task, args.dry_run) != 0
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
