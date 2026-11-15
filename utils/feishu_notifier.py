"""飞书机器人通知（收盘选股等模块使用）。

以前这里直接用 requests.post 裸发，既不支持加签、也不重试、还把
``code`` 缺失当成成功。现在统一走 ``utils.feishu_webhook``，与告警、
新闻、盘中卡片共用同一套凭据口径。
"""
import logging
import os

from utils import feishu_webhook as fw

logger = logging.getLogger(__name__)


class FeishuNotifier:
    def __init__(self, webhook_url: str = ""):
        self.webhook_url = (webhook_url or "").strip()
        # 机器人开启「加签」时配置 FEISHU_SECRET；留空则与旧行为完全一致
        self.secret = (os.environ.get("FEISHU_SECRET") or "").strip()

    def _send(self, payload: dict) -> bool:
        if not self.webhook_url:
            logger.warning("飞书 webhook_url 未配置")
            return False
        try:
            result = fw.send_payload(self.webhook_url, self.secret, payload)
        except Exception as exc:  # noqa: BLE001
            logger.error("飞书推送异常: %s", exc)
            return False
        if not fw.response_ok(result):
            logger.error("飞书推送失败: %s", result)
            return False
        logger.info("飞书推送成功")
        return True

    def send_text(self, text: str) -> bool:
        return self._send({"msg_type": "text", "content": {"text": text}})

    def send_post(self, title: str, content_lines: list) -> bool:
        post_content = [[{"tag": "text", "text": line}] for line in content_lines]
        return self._send({
            "msg_type": "post",
            "content": {"post": {"zh_cn": {"title": title, "content": post_content}}},
        })
