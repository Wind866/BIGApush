#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨夜补跑收盘选股：只把「交易日闸门」锚定到数据日期，其余完全复用 main.py。

为什么需要它：`main.py run` 里有一道 `is_trading_day(今天)` 的闸门。15:43 的
收盘定稿档一旦被调度延迟拖过午夜（含周五档拖到周六凌晨），运行日已经不是
交易日，闸门直接 `sys.exit(0)` —— 数据明明取得到，却不跑，当天定稿永久丢失。
深夜兜底档等于白铺。

交易日预检（`intraday_selection.py --check-trading-day --catchup`）已经用行情
时间戳确认过「这次运行对应哪一天、该不该跑」，所以这里只负责放行，不再重复
判断，并把 `SESSION_DATE` 留给下游日志/卡片标注。

用法（由 selection.yml 在 catchup=true 时调用）：
    SESSION_DATE=2026-11-13 python scripts/run_close_catchup.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SESSION_DATE = (os.environ.get("SESSION_DATE") or "").strip()
if not SESSION_DATE:
    sys.exit("SESSION_DATE 未设置：应由 workflow 传入交易日预检得到的 data_date")

from utils import trade_date_utils as tdu  # noqa: E402

# main.py 内部用 `from utils.trade_date_utils import is_trading_day` 取值，
# 补丁打在模块属性上即可生效（导入时才查属性）。
tdu.is_trading_day = lambda date_str=None, *a, **k: True  # noqa: E731

print(f"[补跑] SESSION_DATE={SESSION_DATE}：交易日闸门已放行，"
      "按上一交易日的数据出收盘定稿")

import main  # noqa: E402  main.py 在 import 阶段不执行，__main__ 里才调用 main()

# 不传参数：main.py 的 argparse 在无子命令时默认走 run
sys.argv = ["main.py", "run"]
sys.exit(main.main())
