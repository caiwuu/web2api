"""
FastAPI 依赖与公共工具。
"""

from fastapi import HTTPException, Request

from core.chat.handler import ChatHandler
from core.config.repository import ConfigRepository


def get_chat_handler(request: Request) -> ChatHandler:
    """从 app state 取出 ChatHandler。"""
    handler = getattr(request.app.state, "chat_handler", None)
    if handler is None:
        raise HTTPException(status_code=503, detail="服务未就绪")
    return handler


def get_config_repo(request: Request) -> ConfigRepository:
    """从 app state 取出 ConfigRepository。"""
    repo = getattr(request.app.state, "config_repo", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="服务未就绪")
    return repo
