"""
配置 API：GET/PUT /api/config；配置页 GET /config。
"""

import logging
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from core.admin.auth import (
    ADMIN_SESSION_COOKIE,
    admin_logged_in,
    check_admin_login_rate_limit,
    configured_config_secret_hash,
    record_admin_login_failure,
    record_admin_login_success,
    require_config_login,
    require_config_login_enabled,
    verify_config_secret,
)
from core.chat.handler import ChatHandler
from core.config.repository import ConfigRepository
from core.http.dependencies import get_config_repo
from core.plugin.base import PluginRegistry

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


class AdminLoginRequest(BaseModel):
    secret: str


def create_config_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/types")
    def get_types(_: None = Depends(require_config_login)) -> list[str]:
        """返回已注册的 type 列表，供配置页 type 下拉使用。"""
        return PluginRegistry.all_types()

    @router.get("/api/config")
    def get_config(
        _: None = Depends(require_config_login),
        repo: ConfigRepository = Depends(get_config_repo),
    ) -> list[dict[str, Any]]:
        """获取配置（代理组 + 账号 name/type/auth）。"""
        return repo.load_raw()

    @router.get("/api/config/status")
    def get_config_status(
        request: Request,
        _: None = Depends(require_config_login),
        repo: ConfigRepository = Depends(get_config_repo),
    ) -> dict[str, Any]:
        """返回配置页需要的账号运行时状态。"""
        handler: ChatHandler | None = getattr(request.app.state, "chat_handler", None)
        if handler is None:
            raise HTTPException(status_code=503, detail="服务未就绪")
        runtime_status = handler.get_account_runtime_status()
        now = int(time.time())
        accounts: dict[str, dict[str, Any]] = {}
        for group in repo.load_groups():
            for account in group.accounts:
                account_id = f"{group.fingerprint_id}:{account.name}"
                runtime = runtime_status.get(account_id, {})
                is_frozen = (
                    account.unfreeze_at is not None and int(account.unfreeze_at) > now
                )
                accounts[account_id] = {
                    "fingerprint_id": group.fingerprint_id,
                    "account_name": account.name,
                    "enabled": account.enabled,
                    "unfreeze_at": account.unfreeze_at,
                    "is_frozen": is_frozen,
                    "is_active": bool(runtime.get("is_active")),
                    "tab_state": runtime.get("tab_state"),
                    "accepting_new": runtime.get("accepting_new"),
                    "active_requests": runtime.get("active_requests", 0),
                }
        return {"now": now, "accounts": accounts}

    @router.put("/api/config")
    async def put_config(
        request: Request,
        config: list[dict[str, Any]],
        _: None = Depends(require_config_login),
        repo: ConfigRepository = Depends(get_config_repo),
    ) -> dict[str, Any]:
        """更新配置并立即生效。"""
        if not config:
            raise HTTPException(status_code=400, detail="配置不能为空")
        for i, g in enumerate(config):
            if not isinstance(g, dict):
                raise HTTPException(status_code=400, detail=f"第 {i + 1} 项应为对象")
            if "fingerprint_id" not in g:
                raise HTTPException(
                    status_code=400, detail=f"代理组 {i + 1} 缺少字段: fingerprint_id"
                )
            use_proxy = g.get("use_proxy", True)
            if isinstance(use_proxy, str):
                use_proxy = use_proxy.strip().lower() not in {
                    "0",
                    "false",
                    "no",
                    "off",
                }
            else:
                use_proxy = bool(use_proxy)
            if use_proxy and not str(g.get("proxy_host", "")).strip():
                raise HTTPException(
                    status_code=400,
                    detail=f"代理组 {i + 1} 启用了代理，需填写 proxy_host",
                )
            accounts = g.get("accounts", [])
            if not accounts:
                raise HTTPException(
                    status_code=400, detail=f"代理组 {i + 1} 至少需要一个账号"
                )
            for j, a in enumerate(accounts):
                if not isinstance(a, dict) or not (a.get("name") or "").strip():
                    raise HTTPException(
                        status_code=400,
                        detail=f"代理组 {i + 1} 账号 {j + 1} 需包含 name",
                    )
                if not (a.get("type") or "").strip():
                    raise HTTPException(
                        status_code=400,
                        detail=f"代理组 {i + 1} 账号 {j + 1} 需包含 type（如 claude）",
                    )
                if "enabled" in a and not isinstance(
                    a.get("enabled"), (bool, int, str)
                ):
                    raise HTTPException(
                        status_code=400,
                        detail=f"代理组 {i + 1} 账号 {j + 1} 的 enabled 类型无效",
                    )
        try:
            repo.save_raw(config)
        except Exception as e:
            logger.exception("保存配置失败")
            raise HTTPException(status_code=400, detail=str(e)) from e
        # 立即生效：重新加载池并替换 chat_handler
        try:
            groups = repo.load_groups()
            handler: ChatHandler | None = getattr(
                request.app.state, "chat_handler", None
            )
            if handler is None:
                raise RuntimeError("chat_handler 未初始化")
            await handler.refresh_configuration(groups, config_repo=repo)
        except Exception as e:
            logger.exception("重载账号池失败")
            raise HTTPException(
                status_code=500, detail=f"配置已保存但重载失败: {e}"
            ) from e
        return {"status": "ok", "message": "配置已保存并生效"}

    @router.get("/login", response_model=None)
    def login_page(request: Request) -> FileResponse | RedirectResponse:
        require_config_login_enabled()
        if admin_logged_in(request):
            return RedirectResponse(url="/config", status_code=302)
        path = STATIC_DIR / "login.html"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="登录页未就绪")
        return FileResponse(path, headers=NO_CACHE_HEADERS)

    @router.post("/api/admin/login", response_model=None)
    def admin_login(payload: AdminLoginRequest, request: Request) -> Response:
        require_config_login_enabled()
        check_admin_login_rate_limit(request)
        secret = payload.secret.strip()
        encoded = configured_config_secret_hash()
        if not secret or not encoded or not verify_config_secret(secret, encoded):
            lock_seconds = record_admin_login_failure(request)
            if lock_seconds > 0:
                raise HTTPException(
                    status_code=429,
                    detail=f"登录失败次数过多，请 {lock_seconds} 秒后再试",
                )
            raise HTTPException(status_code=401, detail="登录失败，secret 不正确")
        record_admin_login_success(request)
        store = request.app.state.admin_sessions
        token = store.create()
        response = JSONResponse({"status": "ok"})
        response.set_cookie(
            key=ADMIN_SESSION_COOKIE,
            value=token,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            max_age=store.ttl_seconds,
            path="/",
        )
        return response

    @router.post("/api/admin/logout", response_model=None)
    def admin_logout(request: Request) -> Response:
        token = (request.cookies.get(ADMIN_SESSION_COOKIE) or "").strip()
        store = getattr(request.app.state, "admin_sessions", None)
        if store is not None:
            store.revoke(token)
        response = JSONResponse({"status": "ok"})
        response.delete_cookie(ADMIN_SESSION_COOKIE, path="/")
        return response

    @router.get("/config", response_model=None)
    def config_page(request: Request) -> FileResponse | RedirectResponse:
        """配置页入口。"""
        require_config_login_enabled()
        if not admin_logged_in(request):
            return RedirectResponse(url="/login", status_code=302)
        path = STATIC_DIR / "config.html"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="配置页未就绪")
        return FileResponse(path, headers=NO_CACHE_HEADERS)

    return router
