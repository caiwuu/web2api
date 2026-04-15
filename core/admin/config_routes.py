"""
配置 API：Identity 管理（上传/删除/扫描/列表）；配置页 GET /config。
"""

import logging
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile
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
from core.identity.manager import (
    install_identity_from_zip,
    remove_identity,
    scan_identities,
)
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


async def _refresh_handler(request: Request, repo: ConfigRepository) -> None:
    """重载账号池并使配置立即生效。"""
    groups = repo.load_groups()
    handler: ChatHandler | None = getattr(request.app.state, "chat_handler", None)
    if handler is None:
        raise RuntimeError("chat_handler 未初始化")
    await handler.refresh_configuration(groups, config_repo=repo)


def create_config_router() -> APIRouter:
    router = APIRouter()

    # -------------------------------------------------------------------
    # Identity 管理 API
    # -------------------------------------------------------------------

    @router.get("/api/types")
    def get_types(_: None = Depends(require_config_login)) -> list[str]:
        return PluginRegistry.all_types()

    @router.get("/api/identities")
    def list_identities(
        request: Request,
        _: None = Depends(require_config_login),
        repo: ConfigRepository = Depends(get_config_repo),
    ) -> dict[str, Any]:
        """列出所有已安装的 identity 及运行时状态。"""
        identities = repo.load_identities()
        handler: ChatHandler | None = getattr(
            request.app.state, "chat_handler", None
        )
        runtime_status = (
            handler.get_account_runtime_status() if handler else {}
        )
        now = int(time.time())
        items: list[dict[str, Any]] = []
        for identity in identities:
            type_statuses: dict[str, dict[str, Any]] = {}
            for t in identity.types:
                account_id = f"{identity.fingerprint_id}:{identity.fingerprint_id}"
                rt_key = f"{account_id}::{t}"
                rt = runtime_status.get(rt_key, {})
                type_statuses[t] = {
                    "is_active": bool(rt.get("is_active")),
                    "tab_state": rt.get("tab_state"),
                    "accepting_new": rt.get("accepting_new"),
                    "active_requests": rt.get("active_requests", 0),
                    "frozen_until": rt.get("frozen_until"),
                }
            items.append(
                {
                    "fingerprint_id": identity.fingerprint_id,
                    "timezone": identity.timezone,
                    "proxy_url": _mask_proxy_url(identity.proxy_url),
                    "types": identity.types,
                    "enabled": identity.enabled,
                    "type_statuses": type_statuses,
                }
            )
        return {"now": now, "identities": items}

    @router.post("/api/identities/upload")
    async def upload_identity(
        request: Request,
        file: UploadFile,
        _: None = Depends(require_config_login),
        repo: ConfigRepository = Depends(get_config_repo),
    ) -> dict[str, Any]:
        """上传 identity zip 文件并安装。"""
        if not file.filename or not file.filename.lower().endswith(".zip"):
            raise HTTPException(status_code=400, detail="请上传 .zip 文件")
        try:
            identity = install_identity_from_zip(file.file)
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception("安装 identity 失败")
            raise HTTPException(
                status_code=500, detail=f"安装失败: {e}"
            ) from e

        repo.save_identity(identity)
        try:
            await _refresh_handler(request, repo)
        except Exception as e:
            logger.exception("重载配置失败")
            raise HTTPException(
                status_code=500, detail=f"已安装但重载失败: {e}"
            ) from e
        return {
            "status": "ok",
            "fingerprint_id": identity.fingerprint_id,
            "types": identity.types,
        }

    @router.delete("/api/identities/{fingerprint_id}")
    async def delete_identity(
        fingerprint_id: str,
        request: Request,
        _: None = Depends(require_config_login),
        repo: ConfigRepository = Depends(get_config_repo),
    ) -> dict[str, Any]:
        """删除 identity：先关浏览器，再删 DB 和文件。"""
        repo.delete_identity(fingerprint_id)
        try:
            await _refresh_handler(request, repo)
        except Exception as e:
            logger.exception("重载配置失败")
        # 浏览器已关闭（prune 会关掉 group 不存在的浏览器），再清理文件
        remove_identity(fingerprint_id)
        return {"status": "ok"}

    @router.patch("/api/identities/{fingerprint_id}")
    async def patch_identity(
        fingerprint_id: str,
        body: dict[str, Any],
        request: Request,
        _: None = Depends(require_config_login),
        repo: ConfigRepository = Depends(get_config_repo),
    ) -> dict[str, Any]:
        """启用/禁用某个 identity。"""
        if "enabled" in body:
            repo.set_identity_enabled(fingerprint_id, bool(body["enabled"]))
        try:
            await _refresh_handler(request, repo)
        except Exception as e:
            logger.exception("重载配置失败")
        return {"status": "ok"}

    @router.post("/api/identities/scan")
    async def scan_and_register(
        request: Request,
        _: None = Depends(require_config_login),
        repo: ConfigRepository = Depends(get_config_repo),
    ) -> dict[str, Any]:
        """扫描 fp-data 目录，发现并注册未入库的 identity。"""
        found = scan_identities()
        existing = {i.fingerprint_id for i in repo.load_identities()}
        registered: list[str] = []
        for identity in found:
            if identity.fingerprint_id not in existing:
                repo.save_identity(identity)
                registered.append(identity.fingerprint_id)
        if registered:
            try:
                await _refresh_handler(request, repo)
            except Exception as e:
                logger.exception("重载配置失败")
        return {"status": "ok", "registered": registered}

    # -------------------------------------------------------------------
    # 兼容旧 API（返回 identity 视角数据）
    # -------------------------------------------------------------------

    @router.get("/api/config")
    def get_config(
        _: None = Depends(require_config_login),
        repo: ConfigRepository = Depends(get_config_repo),
    ) -> list[dict[str, Any]]:
        return repo.load_raw()

    @router.get("/api/config/status")
    def get_config_status(
        request: Request,
        _: None = Depends(require_config_login),
        repo: ConfigRepository = Depends(get_config_repo),
    ) -> dict[str, Any]:
        handler: ChatHandler | None = getattr(
            request.app.state, "chat_handler", None
        )
        if handler is None:
            raise HTTPException(status_code=503, detail="服务未就绪")
        runtime_status = handler.get_account_runtime_status()
        now = int(time.time())
        accounts: dict[str, dict[str, Any]] = {}
        for group in repo.load_groups():
            for account in group.accounts:
                account_id = f"{group.fingerprint_id}:{account.name}"
                rt_key = f"{account_id}::{account.type}"
                runtime = runtime_status.get(rt_key, {})
                is_frozen = (
                    account.unfreeze_at is not None
                    and int(account.unfreeze_at) > now
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

    # -------------------------------------------------------------------
    # 管理员登录/登出 & 配置页
    # -------------------------------------------------------------------

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
            raise HTTPException(
                status_code=401, detail="登录失败，secret 不正确"
            )
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
        require_config_login_enabled()
        if not admin_logged_in(request):
            return RedirectResponse(url="/login", status_code=302)
        path = STATIC_DIR / "config.html"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="配置页未就绪")
        return FileResponse(path, headers=NO_CACHE_HEADERS)

    return router


def _mask_proxy_url(url: str) -> str:
    """脱敏代理 URL：隐藏用户名前缀（-region 之前）和密码。

    caiwu123-region-GB-...:caiwu123@host → ****-region-GB-...:****@host
    """
    if not url or "@" not in url:
        return url
    try:
        scheme_rest = url.split("://", 1)
        if len(scheme_rest) != 2:
            return url
        scheme, rest = scheme_rest
        userinfo, host = rest.split("@", 1)
        user, _, password = userinfo.partition(":")
        # 用户名中 -region 之前的部分脱敏
        idx = user.find("-region")
        if idx > 0:
            user = "****" + user[idx:]
        else:
            user = "****"
        return f"{scheme}://{user}:****@{host}"
    except Exception:
        return url
