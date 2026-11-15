#!/usr/bin/env python3
"""工作流失败告警（支持加签），替代各 workflow 里的裸 curl。

裸 curl 有两个致命问题：不发 ``sign`` 字段（机器人开加签必被拒，19021），
且不解析响应体（HTTP 200 + ``{"code":19021}`` 也被当作成功）。结果就是
「任务失败了、告警也发不出去、运行页还显示告警步骤成功」的三重静默。

用法：
    python scripts/alert.py "⚠️ 早间新闻任务运行失败，请查看日志" --run-url "$RUN_URL"

环境变量（按优先级）：
    ALERT_WEBHOOK   独立告警机器人（推荐）
    FEISHU_WEBHOOK  业务机器人（兜底）
    ALERT_SECRET    独立告警机器人的加签密钥，未配回落 FEISHU_SECRET
    FEISHU_SECRET   业务机器人加签密钥

退出码：0 发送成功 / 未配置 webhook（视为无需告警）；1 发送失败。
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from utils import feishu_webhook as fw  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass


def main(argv: list[str]) -> int:
    text = ""
    if len(argv) > 1 and not argv[1].startswith("-"):
        text = argv[1]
    for i, arg in enumerate(argv):
        if arg == "--run-url" and i + 1 < len(argv):
            text = f"{text}\n{argv[i + 1]}".strip()

    if not fw.resolve_credentials(alert=True)[0]:
        print("[告警] 未配置 ALERT_WEBHOOK / FEISHU_WEBHOOK，跳过告警")
        return 0
    if not (os.environ.get("ALERT_WEBHOOK") or "").strip():
        print("[告警] 未配置独立告警机器人 ALERT_WEBHOOK，退回业务 webhook"
              "（业务 webhook 故障时本次告警也会一起失效）")

    ok, detail = fw.alert_text(text or "运行失败，请查看日志")
    print(f"[告警] {detail}")
    return 0 if ok or detail == "未配置 webhook" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
