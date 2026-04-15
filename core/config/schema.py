"""
配置数据模型：Identity（浏览器状态快照）驱动，按指纹分组。

一个 Identity = 一个 fingerprint_id = 一个浏览器实例，可同时登录多个站点。
Identity 通过 info.json 描述元数据，浏览器状态文件直接存储在 user-data-dir 中。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, unquote


@dataclass(frozen=True)
class AccountConfig:
    """单个账号：名称、类别、认证 JSON。一个账号只属于一个 type。"""

    name: str
    type: str  # 如 claude, chatgpt, kimi
    auth: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    unfreeze_at: int | None = (
        None  # Unix 时间戳，接口返回的解冻时间；None 或已过则视为可用
    )

    def auth_json(self) -> str:
        """序列化为 JSON 字符串供 DB 存储。"""
        import json

        return json.dumps(self.auth, ensure_ascii=False)

    def is_available(self) -> bool:
        """已启用且当前时间 >= 解冻时间则可用（无解冻时间视为可用）。"""
        if not self.enabled:
            return False
        if self.unfreeze_at is None:
            return True
        import time

        return time.time() >= self.unfreeze_at


@dataclass
class ProxyGroupConfig:
    """一个代理 IP 组：代理参数 + 指纹 + 下属账号列表。"""

    proxy_host: str
    proxy_user: str
    proxy_pass: str
    fingerprint_id: str
    use_proxy: bool = True
    timezone: str | None = None
    accounts: list[AccountConfig] = field(default_factory=list)

    def account_ids(self) -> list[str]:
        return [a.name for a in self.accounts]


# ---------------------------------------------------------------------------
# Identity — 浏览器状态快照配置
# ---------------------------------------------------------------------------


@dataclass
class IdentityConfig:
    """一个 identity：fingerprint_id + 代理 + 支持的 type 列表。"""

    fingerprint_id: str
    timezone: str
    proxy_url: str  # socks5://user:pass@host:port
    types: list[str]  # ["claude", "chatgpt", "deepseek"]
    enabled: bool = True


def parse_proxy_url(proxy_url: str) -> tuple[str, str, str]:
    """
    从 socks5://user:pass@host:port 解析出 (proxy_host, proxy_user, proxy_pass)。
    proxy_host 格式为 host:port（与 ProxyGroupConfig / LocalProxyForwarder 一致）。
    """
    parsed = urlparse(proxy_url)
    host = parsed.hostname or ""
    port = parsed.port or 0
    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    proxy_host = f"{host}:{port}" if port else host
    return proxy_host, user, password


def identity_to_proxy_group(identity: IdentityConfig) -> ProxyGroupConfig:
    """将 IdentityConfig 映射为下游调度层使用的 ProxyGroupConfig。

    每个 type 生成一个 AccountConfig（auth 为空——identity 模式不需要注入凭证）。
    account.name 使用 fingerprint_id 以保持 account_id 格式为 "fid:fid"，
    每 type 一个 AccountConfig 使调度器能独立管理各 type 的 tab。
    """
    proxy_host, proxy_user, proxy_pass = parse_proxy_url(identity.proxy_url)
    use_proxy = bool(proxy_host)
    accounts = [
        AccountConfig(
            name=identity.fingerprint_id,
            type=t,
            auth={},
            enabled=identity.enabled,
        )
        for t in identity.types
    ]
    return ProxyGroupConfig(
        proxy_host=proxy_host,
        proxy_user=proxy_user,
        proxy_pass=proxy_pass,
        fingerprint_id=identity.fingerprint_id,
        use_proxy=use_proxy,
        timezone=identity.timezone,
        accounts=accounts,
    )


def account_from_row(
    name: str,
    type: str,
    auth_json: str,
    enabled: bool = True,
    unfreeze_at: int | None = None,
) -> AccountConfig:
    """从 DB 行构造 AccountConfig。"""
    import json

    try:
        auth = json.loads(auth_json) if auth_json else {}
    except Exception:
        auth = {}
    return AccountConfig(
        name=name,
        type=type,
        auth=auth,
        enabled=enabled,
        unfreeze_at=unfreeze_at,
    )
