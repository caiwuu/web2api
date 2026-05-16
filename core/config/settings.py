"""
统一的 YAML 配置加载。

优先级：
1. WEB2API_CONFIG_PATH 指定的路径
2. 项目根目录下的 config.local.yaml
3. 项目根目录下的 config.yaml
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_ENV_KEY = "WEB2API_CONFIG_PATH"
_LOCAL_CONFIG_NAME = "config.local.yaml"
_DEFAULT_CONFIG_NAME = "config.yaml"


def _resolve_config_path() -> Path:
    configured = os.environ.get(_CONFIG_ENV_KEY, "").strip()
    if configured:
        return Path(configured).expanduser()
    local_config = _PROJECT_ROOT / _LOCAL_CONFIG_NAME
    if local_config.exists():
        return local_config
    return _PROJECT_ROOT / _DEFAULT_CONFIG_NAME


_CONFIG_PATH = _resolve_config_path()

_config_cache: dict[str, Any] | None = None


def get_config_path() -> Path:
    return _CONFIG_PATH


def reset_cache() -> None:
    global _config_cache
    _config_cache = None


def load_config() -> dict[str, Any]:
    """按优先级加载配置文件，不存在时返回空 dict。"""
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    if not _CONFIG_PATH.exists():
        _config_cache = {}
        return {}
    try:
        with _CONFIG_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            _config_cache = {}
        else:
            _config_cache = dict(data)
    except Exception:
        _config_cache = {}
    return _config_cache


def get(section: str, key: str, default: Any = None) -> Any:
    """从 config 读取 section.key，不存在则返回 default。"""
    cfg = load_config().get(section) or {}
    if not isinstance(cfg, dict):
        return default
    val = cfg.get(key)
    return val if val is not None else default


def get_bool(section: str, key: str, default: bool = False) -> bool:
    """从 config 读取布尔值，兼容 true/false、1/0、yes/no、on/off。"""
    val = get(section, key, default)
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        normalized = val.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(default)


def get_float(section: str, key: str, default: float = 0.0) -> float:
    """从 config 读取浮点数；缺失或非法时返回 default。"""
    val = get(section, key, default)
    try:
        return float(val)
    except (TypeError, ValueError):
        return float(default)
