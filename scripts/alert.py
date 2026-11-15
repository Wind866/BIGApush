#!/usr/bin/env python3
"""工作流失败告警（支持加签），替代各 workflow 里的裸 curl。

裸 curl 有两个致命问题：不发 ``sign`` 字段（机器人开加签必被拒，19021），
且不解析响应体（HTTP 200 + ``{"code":19021}`` 也被当作成功）。结果就是
「任务失败了、告警也发不出去、运行页还显示告警步骤成功」的三重静默。

用法：
    python scripts/alert.py "⚠️ 早间新闻任务运行失败，请查看日志" --run-url "$RUN_URL"
    python scripts/alert.py --check     # 只体检凭据配置，不发消息

环境变量（按优先级）：
    ALERT_WEBHOOK   独立告警机器人（推荐）
    FEISHU_WEBHOOK  业务机器人（兜底）
    ALERT_SECRET    独立告警机器人的加签密钥，未配回落 FEISHU_SECRET
    FEISHU_SECRET   业务机器人加签密钥

退出码：0 发送成功 / 未配置 webhook（视为无需告警）；1 发送失败
（--check 模式：0 凭据齐全；1 连 webhook 都没配）。
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


def _masked(name: str) -> str:
    """只报「配了没 / 多长」，绝不回显密钥本身。"""
    value = (os.environ.get(name) or "").strip()
    return f"已配置（{len(value)} 字符）" if value else "未配置"


def check_env() -> int:
    """--check：只体检凭据，不发消息。

    加签开关在飞书群那一侧，仓库这边看不到，所以这里只能核对「密钥配了没」，
    并把不对称配置的后果说清楚：只开一半加签，业务推送被拒 → 触发失败告警 →
    告警机器人同样要求加签 → 告警也发不出去，群里一条消息都没有。
    """
    alert_hook = fw.resolve_credentials(alert=True)[0]
    biz_hook, biz_secret = fw.resolve_credentials()
    # 是否真的配了独立告警机器人：resolve_credentials 会回落业务 webhook，
    # 用它判断会把「只有一个业务机器人」误报成「独立告警已就绪」。
    raw_alert = (os.environ.get("ALERT_WEBHOOK") or "").strip()

    print("[体检] 推送凭据（只显示是否配置，不回显内容）")
    for name in ("FEISHU_WEBHOOK", "FEISHU_SECRET", "ALERT_WEBHOOK", "ALERT_SECRET"):
        print(f"  {name:<14} {_masked(name)}")

    if not alert_hook and not biz_hook:
        print("❌ 两个 webhook 都没配：业务推送与失败告警都发不出去")
        return 1
    if biz_hook and not biz_secret:
        print("⚠️ 业务机器人没有加签密钥：该机器人一旦开启加签，推送会被拒"
              "（19021，HTTP 仍 200）")
    if raw_alert and not (os.environ.get("ALERT_SECRET") or "").strip():
        print("⚠️ 告警机器人没有自己的加签密钥"
              + ("（会回落 FEISHU_SECRET）" if biz_secret else "，且 FEISHU_SECRET 也没配"))
    if not biz_hook:
        print("ℹ️ 未配 FEISHU_WEBHOOK：业务推送会被跳过（数据照常生成）")
    if raw_alert:
        print("✅ 独立告警机器人已配置：业务 webhook 故障时告警仍能发出")
    else:
        print("ℹ️ 未配 ALERT_WEBHOOK：告警回落到业务 webhook，业务通道故障时告警一起失效")
    print("提示：机器人是否开启加签要在「飞书群 → 设置 → 群机器人 → 该机器人」里看，"
          "仓库侧只能核对密钥。")
    return 0


def main(argv: list[str]) -> int:
    if "--check" in argv:
        return check_env()

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
