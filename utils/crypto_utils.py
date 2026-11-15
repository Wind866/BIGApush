#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
加密工具类 - 提供对称加密解密功能
"""

import base64
from cryptography.fernet import Fernet

class CryptoUtils:
    """加密工具类

    ⚠️ 不再内置默认密钥。旧的 DEFAULT_KEY 是 53 字节占位串，不是合法的 Fernet
    密钥（必须 44 字符 urlsafe-base64）；`Fernet(key)` 抛 ValueError 后被静默
    catch，改用**一次性随机密钥**加密并把密钥随手丢掉 —— 密文永远解不开，而且
    不报错。现在改成显式失败：宁可在这里炸掉，也不要留下一堆读不回来的密文。
    """

    # 已移除的占位密钥。保留属性只为让遗留引用立刻暴露（None 会直接报错，
    # 而不是被当成一个"看起来能用"的密钥继续加密）。
    DEFAULT_KEY = None

    @staticmethod
    def _build_fernet(key, action: str) -> Fernet:
        """统一校验密钥；不合法就报错，绝不静默换密钥。"""
        if key is None:
            raise RuntimeError(
                f"CryptoUtils.{action}() 必须显式传入密钥（44 字符 urlsafe-base64，"
                "可用 CryptoUtils.generate_key() 生成）。内置默认密钥已移除：旧实现"
                "会静默改用一次性随机密钥加密，密钥随即丢失，密文永远解不开。"
            )
        if isinstance(key, str):
            key = key.encode("utf-8")
        try:
            return Fernet(key)
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"CryptoUtils.{action}() 的密钥不合法，需要 44 字符 urlsafe-base64"
                f"（当前 {len(key) if isinstance(key, (bytes, str)) else type(key).__name__}）：{exc}"
            ) from exc

    @staticmethod
    def generate_key() -> bytes:
        """生成加密密钥

        Returns:
            32字节的URL安全base64编码密钥
        """
        return Fernet.generate_key()

    @staticmethod
    def encrypt(data: str, key: bytes = None) -> str:
        """加密字符串

        Args:
            data: 要加密的数据
            key: 加密密钥（必填，44 字符 urlsafe-base64）

        Returns:
            加密后的base64字符串

        Raises:
            RuntimeError: 未传密钥
            ValueError: 密钥格式不合法
        """
        fernet = CryptoUtils._build_fernet(key, "encrypt")
        encrypted = fernet.encrypt(data.encode('utf-8'))
        return base64.urlsafe_b64encode(encrypted).decode('utf-8')

    @staticmethod
    def decrypt(encrypted_data: str, key: bytes = None) -> str:
        """解密字符串

        保留「解不开就原样返回」的宽容语义：调用方（授权配置读取）靠这个降级，
        密文/密钥不匹配时不该让整个流程崩掉。

        Args:
            encrypted_data: 加密后的base64字符串
            key: 解密密钥（44 字符 urlsafe-base64）

        Returns:
            解密后的原始数据；密钥缺失或不匹配时返回原文
        """
        try:
            fernet = CryptoUtils._build_fernet(key, "decrypt")
            encrypted_bytes = base64.urlsafe_b64decode(encrypted_data)
            decrypted = fernet.decrypt(encrypted_bytes)
            return decrypted.decode('utf-8')
        except Exception:
            # 如果解密失败，返回原始数据
            return encrypted_data

# 测试
if __name__ == '__main__':
    # 生成密钥
    key = CryptoUtils.generate_key()
    print("生成的密钥:", key.decode('utf-8'))
    
    # 测试加密解密
    original = "test_file.json"
    encrypted = CryptoUtils.encrypt(original, key)
    decrypted = CryptoUtils.decrypt(encrypted, key)
    
    print(f"原始数据: {original}")
    print(f"加密后: {encrypted}")
    print(f"解密后: {decrypted}")
    print(f"解密成功: {original == decrypted}")
