"""简易会话管理 —— 存储已登录的邮箱凭证"""
import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field

_lock_ttl_seconds = 30 * 24 * 3600  # 会话保留 30 天

# 简单的对称加密密钥（基于进程启动时的随机值，仅用于数据库中授权码的混淆存储）
# 部署上线时应替换为环境变量中的固定密钥
import os
_ENC_KEY = os.getenv("CRM_ENC_KEY", "crm-default-enc-key-v1")


def encrypt(text: str) -> str:
    """简单加密授权码，用于存入数据库"""
    if not text:
        return ""
    key_bytes = _ENC_KEY.encode("utf-8")
    text_bytes = text.encode("utf-8")
    result = bytearray()
    for i, b in enumerate(text_bytes):
        result.append(b ^ key_bytes[i % len(key_bytes)])
    return base64.b64encode(bytes(result)).decode("utf-8")


def decrypt(encrypted: str) -> str:
    """解密授权码"""
    if not encrypted:
        return ""
    try:
        key_bytes = _ENC_KEY.encode("utf-8")
        data = base64.b64decode(encrypted)
        result = bytearray()
        for i, b in enumerate(data):
            result.append(b ^ key_bytes[i % len(key_bytes)])
        return result.decode("utf-8")
    except Exception:
        return ""


@dataclass
class Session:
    token: str
    email: str
    password: str            # IMAP 授权码/密码（内存中保留，用于实时拉取）
    provider_id: str
    imap_host: str
    imap_port: int
    account_id: int | None = None    # 关联的数据库账号 ID
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        return time.time() - self.created_at > _lock_ttl_seconds


# 进程级会话表: token -> Session
_sessions: dict[str, Session] = {}


def create_session(email: str, password: str, provider_id: str,
                   imap_host: str, imap_port: int,
                   account_id: int | None = None) -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = Session(
        token=token,
        email=email,
        password=password,
        provider_id=provider_id,
        imap_host=imap_host,
        imap_port=imap_port,
        account_id=account_id,
    )
    _cleanup_expired()
    return token


def get_session(token: str | None) -> Session | None:
    if not token:
        return None
    s = _sessions.get(token)
    if not s or s.is_expired():
        _sessions.pop(token, None)
        return None
    s.last_active = time.time()
    return s


def destroy_session(token: str | None) -> bool:
    if not token:
        return False
    return _sessions.pop(token, None) is not None


def _cleanup_expired():
    expired = [t for t, s in _sessions.items() if s.is_expired()]
    for t in expired:
        _sessions.pop(t, None)


def mask_email(email: str) -> str:
    """脱敏显示邮箱地址，用于返回给前端"""
    if not email or "@" not in email:
        return email or ""
    name, domain = email.rsplit("@", 1)
    if len(name) <= 2:
        masked = name[0] + "*"
    else:
        masked = name[0] + "*" * (len(name) - 2) + name[-1]
    return f"{masked}@{domain}"
