"""飞书自定义机器人 · 统一发送层（加签 / 重试 / 业务码校验）。

## 为什么要有这一层

本仓库此前有 5 个脚本各自 ``requests.post`` / ``urllib`` 直连 webhook，
7 个工作流用裸 ``curl`` 发失败告警，Cloudflare Worker 里还有第 9 份实现。
它们有共同的三个缺口：

1. **完全不支持加签**。飞书机器人一旦开启「加签」，任何不带 ``timestamp`` /
   ``sign`` 的消息都会被拒（业务码 19021），但 HTTP 状态仍是 200。
2. **不检查业务码**。裸 curl 只看传输层，``{"code":19021}`` 会被当成成功，
   告警在最需要它的时候静默哑掉；部分脚本把 ``code`` 缺失也当作成功。
3. **没有网络重试**。飞书偶发 502 / 连接重置就会让整个档期判失败。

这里把发送收敛成一个标准库实现（无第三方依赖，workflow 里只装 requests
也能用），并让所有调用方复用同一套凭据与校验口径。

## 兼容性

``secret`` 为空时**不发签名字段**，行为与旧版完全一致 —— 机器人没开加签
的仓库无需改任何配置即可平滑升级。

环境变量约定：
    FEISHU_WEBHOOK / FEISHU_SECRET   业务机器人
    ALERT_WEBHOOK  / ALERT_SECRET    独立告警机器人（优先于业务机器人）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

__all__ = [
    "gen_sign", "sign_payload", "post_json", "response_ok",
    "send_text", "send_payload", "resolve_credentials", "alert_text",
    "describe_rejection", "SIGN_HINT",
]

# 飞书自定义机器人业务码：0 / 缺失 视为成功；19021 加签失败；19024 关键词校验失败
SIGN_ERROR_CODE = 19021
SIGN_HINT = "19021 = 机器人开启了加签，但 FEISHU_SECRET / ALERT_SECRET 为空或不对"


def describe_rejection(result: Any) -> str:
    """把飞书业务码翻成一句能直接照做的提示；非加签错误返回空串。

    调用方拿到的 ``result`` 是飞书原样返回的 JSON，``{"code":19021}`` 单看
    完全看不出「要去配密钥」，所以把这层翻译集中在这里，业务脚本只负责拼接。
    """
    if not isinstance(result, dict):
        return ""
    code = result.get("code", result.get("StatusCode"))
    return f"（{SIGN_HINT}）" if code in (SIGN_ERROR_CODE, str(SIGN_ERROR_CODE)) else ""


def gen_sign(secret: str, timestamp: str) -> str:
    """飞书官方加签算法：HMAC-SHA256(key=f'{ts}\\n{secret}', msg=b'') → base64。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def sign_payload(payload: dict, secret: str) -> dict:
    """返回带签名的**新** payload；secret 为空时原样返回副本。"""
    out = dict(payload)
    secret = (secret or "").strip()
    if secret:
        ts = str(int(time.time()))
        out["timestamp"] = ts
        out["sign"] = gen_sign(secret, ts)
    return out


def response_ok(result: Any) -> bool:
    """飞书响应是否真正受理。注意：HTTP 200 不代表成功。"""
    if not isinstance(result, dict):
        return False
    code = result.get("code", result.get("StatusCode"))
    return code in (0, None)


def post_json(webhook: str, payload: dict, secret: str = "",
              timeout: float = 12, attempts: int = 2) -> dict:
    """POST 一个 JSON 并解析响应。

    - 网络异常 / 429 / 5xx：重试 ``attempts`` 次
    - 4xx（非 429）：立即抛错，重试无意义
    - 业务错误（HTTP 200 但 ``code != 0``）：不重试，原样返回给调用方判断

    返回解析后的 dict；失败抛最后一次异常。
    """
    webhook = (webhook or "").strip()
    if not webhook:
        raise ValueError("webhook 为空")
    body = json.dumps(sign_payload(payload, secret), ensure_ascii=False).encode("utf-8")
    last: Exception | None = None
    for i in range(max(1, attempts)):
        req = urllib.request.Request(
            webhook, data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            if exc.code < 500 and exc.code != 429:
                raise
            last = exc
        except Exception as exc:  # noqa: BLE001 - 网络抖动统一重试
            last = exc
        if i + 1 < attempts:
            time.sleep(1.0 + i)
    raise last if last else RuntimeError("飞书发送失败")


def send_text(webhook: str, secret: str, text: str, timeout: float = 12) -> dict:
    """纯文本消息，用于业务推送与失败告警（同样走加签）。"""
    return post_json(
        webhook,
        {"msg_type": "text", "content": {"text": text}},
        secret, timeout,
    )


def send_payload(webhook: str, secret: str, payload: dict,
                 timeout: float = 12) -> dict:
    """交互式卡片 / 任意自定义 payload。"""
    return post_json(webhook, payload, secret, timeout)


def resolve_credentials(alert: bool = False) -> tuple[str, str]:
    """按优先级取 (webhook, secret)。

    alert=True 时优先独立告警机器人，未配置则回落业务机器人 —— 与旧版
    ``HOOK="${ALERT_WEBHOOK:-$FEISHU_WEBHOOK}"`` 语义一致。
    """
    if alert:
        hook = (os.environ.get("ALERT_WEBHOOK") or "").strip() \
            or (os.environ.get("FEISHU_WEBHOOK") or "").strip()
        secret = (os.environ.get("ALERT_SECRET") or "").strip() \
            or (os.environ.get("FEISHU_SECRET") or "").strip()
    else:
        hook = (os.environ.get("FEISHU_WEBHOOK") or "").strip()
        secret = (os.environ.get("FEISHU_SECRET") or "").strip()
    return hook, secret


def alert_text(text: str, alert: bool = True, timeout: float = 12) -> tuple[bool, str]:
    """发一条告警/文本消息，返回 (是否成功, 说明)。

    未配置 webhook → (False, 说明)，**绝不返回成功** —— 否则上层会把
    「没发出去」记成台账里的「本档期已推送」，后续补跑被幂等守卫跳过。
    """
    hook, secret = resolve_credentials(alert=alert)
    if not hook:
        return False, "未配置 webhook"
    try:
        result = send_text(hook, secret, text, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    if not response_ok(result):
        return False, f"飞书拒绝受理：{result}{describe_rejection(result)}"
    return True, "已发送"
