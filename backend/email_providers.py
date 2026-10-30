"""主流邮箱服务商 IMAP/SMTP 配置"""
from dataclasses import dataclass, field


@dataclass
class ProviderConfig:
    id: str
    name: str
    imap_host: str
    imap_port: int
    auth_code_label: str  # 提示文案：授权码 / 密码
    help_url: str         # 获取授权码的帮助链接
    domain_hints: list    # 邮箱后缀，用于自动识别
    webmail_url: str = ""     # 网页版邮箱地址（用于跳转登录/发邮件）
    compose_url: str = ""     # 直接打开写邮件页面的 URL 模板


# ── 常见邮箱服务商 ──
PROVIDERS: dict[str, ProviderConfig] = {
    "qq": ProviderConfig(
        id="qq",
        name="QQ 邮箱",
        imap_host="imap.qq.com",
        imap_port=993,
        auth_code_label="授权码",
        help_url="https://service.mail.qq.com/detail/0/75",
        domain_hints=["qq.com", "vip.qq.com", "foxmail.com"],
        webmail_url="https://mail.qq.com",
        compose_url="https://mail.qq.com/cgi-bin/frame_html?sid=&template=composemail",
    ),
    "163": ProviderConfig(
        id="163",
        name="网易 163 邮箱",
        imap_host="imap.163.com",
        imap_port=993,
        auth_code_label="授权码",
        help_url="https://help.mail.163.com/faqDetail.do?code=d75a8f59d6de45e1a388b4253d6d6c23",
        domain_hints=["163.com", "vip.163.com"],
        webmail_url="https://mail.163.com",
        compose_url="https://mail.163.com/js6/main.jsp?func=mbox:compose",
    ),
    "126": ProviderConfig(
        id="126",
        name="网易 126 邮箱",
        imap_host="imap.126.com",
        imap_port=993,
        auth_code_label="授权码",
        help_url="https://help.mail.163.com/faqDetail.do?code=d75a8f59d6de45e1a388b4253d6d6c23",
        domain_hints=["126.com", "vip.126.com"],
        webmail_url="https://mail.126.com",
        compose_url="https://mail.126.com/js6/main.jsp?func=mbox:compose",
    ),
    "yeah": ProviderConfig(
        id="yeah",
        name="网易 Yeah 邮箱",
        imap_host="imap.yeah.net",
        imap_port=993,
        auth_code_label="授权码",
        help_url="https://help.mail.163.com/faqDetail.do?code=d75a8f59d6de45e1a388b4253d6d6c23",
        domain_hints=["yeah.net"],
        webmail_url="https://mail.yeah.net",
        compose_url="https://mail.yeah.net/js6/main.jsp?func=mbox:compose",
    ),
    "outlook": ProviderConfig(
        id="outlook",
        name="Outlook / Hotmail",
        imap_host="outlook.office365.com",
        imap_port=993,
        auth_code_label="应用密码",
        help_url="https://support.microsoft.com/account-billing/manage-app-passwords-for-two-step-verification-d6dc8c6d-8bf3-42e2-b7e4-b2b5e6b6d7d6",
        domain_hints=["outlook.com", "hotmail.com", "live.com", "msn.com"],
        webmail_url="https://outlook.live.com",
        compose_url="https://outlook.live.com/mail/0/deeplink/compose",
    ),
    "gmail": ProviderConfig(
        id="gmail",
        name="Gmail",
        imap_host="imap.gmail.com",
        imap_port=993,
        auth_code_label="应用密码",
        help_url="https://support.google.com/accounts/answer/185833",
        domain_hints=["gmail.com", "googlemail.com"],
        webmail_url="https://mail.google.com",
        compose_url="https://mail.google.com/mail/u/0/?view=cm&fs=1",
    ),
    "sina": ProviderConfig(
        id="sina",
        name="新浪邮箱",
        imap_host="imap.sina.com",
        imap_port=993,
        auth_code_label="客户端密码",
        help_url="https://help.sina.com/doc/i/1440.html",
        domain_hints=["sina.com", "vip.sina.com"],
        webmail_url="https://mail.sina.com.cn",
        compose_url="https://mail.sina.com.cn",
    ),
    "sohu": ProviderConfig(
        id="sohu",
        name="搜狐邮箱",
        imap_host="imap.sohu.com",
        imap_port=993,
        auth_code_label="密码",
        help_url="https://help.sohu.com/",
        domain_hints=["sohu.com"],
        webmail_url="https://mail.sohu.com",
        compose_url="https://mail.sohu.com",
    ),
    "aliyun": ProviderConfig(
        id="aliyun",
        name="阿里云邮箱",
        imap_host="imap.aliyun.com",
        imap_port=993,
        auth_code_label="密码",
        help_url="https://help.aliyun.com/document_detail/36693.html",
        domain_hints=["aliyun.com"],
        webmail_url="https://mail.aliyun.com",
        compose_url="https://mail.aliyun.com",
    ),
    "yandex": ProviderConfig(
        id="yandex",
        name="Yandex Mail",
        imap_host="imap.yandex.com",
        imap_port=993,
        auth_code_label="应用密码",
        help_url="https://yandex.com/support/id/authorization/app-passwords.html",
        domain_hints=["yandex.com", "yandex.ru", "narod.ru"],
        webmail_url="https://mail.yandex.com",
        compose_url="https://mail.yandex.com",
    ),
    "custom": ProviderConfig(
        id="custom",
        name="其他 / 自定义",
        imap_host="",
        imap_port=993,
        auth_code_label="密码",
        help_url="",
        domain_hints=[],
        webmail_url="",
        compose_url="",
    ),
}


def list_providers() -> list[dict]:
    return [
        {
            "id": p.id,
            "name": p.name,
            "imap_host": p.imap_host,
            "imap_port": p.imap_port,
            "auth_code_label": p.auth_code_label,
            "help_url": p.help_url,
            "domains": p.domain_hints,
            "webmail_url": p.webmail_url,
            "compose_url": p.compose_url,
        }
        for p in PROVIDERS.values()
    ]


def get_provider(provider_id: str) -> ProviderConfig | None:
    return PROVIDERS.get(provider_id)


def detect_provider(email_address: str) -> str:
    """根据邮箱地址自动判断服务商 ID"""
    addr = (email_address or "").lower()
    if "@" not in addr:
        return "custom"
    domain = addr.rsplit("@", 1)[-1]
    for pid, p in PROVIDERS.items():
        if pid == "custom":
            continue
        if domain in p.domain_hints:
            return pid
    return "custom"
