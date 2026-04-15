"""
Identity 文件管理：安装、卸载、扫描、校验浏览器状态快照。

一个 identity = 一个 fingerprint_id 目录，包含 info.json 和 Chromium user-data-dir
中的身份相关文件（Cookies、Local Storage 等）。
"""

from __future__ import annotations

import json
import logging
import shutil
import zipfile
from pathlib import Path
from typing import IO

from core.config.schema import IdentityConfig
from core.constants import user_data_dir

logger = logging.getLogger(__name__)

IDENTITY_PATHS = [
    "Local State",
    "Default/Cookies",
    "Default/Cookies-journal",
    "Default/Network/Cookies",
    "Default/Network/Cookies-journal",
    "Default/Local Storage",
    "Default/Session Storage",
    "Default/IndexedDB",
    "Default/Web Data",
    "Default/Web Data-journal",
    "Default/History",
    "Default/History-journal",
    "Default/Login Data",
    "Default/Login Data-journal",
    "Default/Visited Links",
    "Default/Extension Cookies",
    "Default/Sync Data",
    "Default/databases",
    "Default/File System",
    "Default/Platform Notifications",
]

INFO_JSON = "info.json"


def validate_info_json(data: dict) -> IdentityConfig:
    """校验 info.json 内容并返回 IdentityConfig，格式不对则抛 ValueError。"""
    fingerprint_id = str(data.get("fingerprint_id", "")).strip()
    if not fingerprint_id:
        raise ValueError("info.json 缺少 fingerprint_id")

    timezone = str(data.get("timezone", "")).strip()
    if not timezone:
        raise ValueError("info.json 缺少 timezone")

    proxy_url = str(data.get("proxy_url", "")).strip()
    if not proxy_url:
        raise ValueError("info.json 缺少 proxy_url")

    types = data.get("type") or data.get("types")
    if isinstance(types, str):
        types = [types]
    if not isinstance(types, list) or not types:
        raise ValueError("info.json 缺少 type（需为非空列表）")
    types = [str(t).strip() for t in types if str(t).strip()]
    if not types:
        raise ValueError("info.json 的 type 列表不能全为空字符串")

    return IdentityConfig(
        fingerprint_id=fingerprint_id,
        timezone=timezone,
        proxy_url=proxy_url,
        types=types,
    )


def _read_info_json(path: Path) -> IdentityConfig:
    """读取并校验一个目录下的 info.json。"""
    info_path = path / INFO_JSON
    if not info_path.is_file():
        raise FileNotFoundError(f"未找到 {info_path}")
    with open(info_path, encoding="utf-8") as f:
        data = json.load(f)
    return validate_info_json(data)


def _copy_entry(src: Path, dst: Path) -> None:
    """复制单条身份路径：目录用 copytree，文件用 copy2。"""
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    else:
        if dst.exists():
            dst.unlink()
        shutil.copy2(src, dst)


def install_identity_from_dir(source_dir: Path) -> IdentityConfig:
    """从一个已解压的目录安装 identity 到 user-data-dir。

    source_dir 中须包含 info.json 和身份文件。
    """
    identity = _read_info_json(source_dir)
    target = user_data_dir(identity.fingerprint_id)
    target.mkdir(parents=True, exist_ok=True)

    for rel in IDENTITY_PATHS:
        src = source_dir / rel
        if src.exists():
            _copy_entry(src, target / rel)
            logger.info("[identity] installed %s → %s", src, target / rel)

    info_dst = target / INFO_JSON
    if not info_dst.exists():
        shutil.copy2(source_dir / INFO_JSON, info_dst)

    logger.info(
        "[identity] installed fingerprint_id=%s types=%s",
        identity.fingerprint_id,
        identity.types,
    )
    return identity


def install_identity_from_zip(zip_file: IO[bytes] | Path) -> IdentityConfig:
    """从 zip 文件安装 identity。

    zip 内部结构可以是:
      1110/info.json, 1110/Default/Cookies, ...   （有顶层目录）
      info.json, Default/Cookies, ...              （无顶层目录）
    macOS 打包的 zip 可能额外包含 __MACOSX 目录，会被自动忽略。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        with zipfile.ZipFile(zip_file, "r") as zf:
            zf.extractall(tmp)

        source = _find_identity_root(tmp)
        return install_identity_from_dir(source)


def _find_identity_root(extracted: Path) -> Path:
    """在解压后的目录中定位包含 info.json 的根目录。"""
    # 直接在根目录找到 info.json
    if (extracted / INFO_JSON).is_file():
        return extracted

    # 过滤掉 __MACOSX、.DS_Store 等 macOS 生成的隐藏条目
    children = [
        d for d in extracted.iterdir()
        if d.is_dir() and not d.name.startswith(("__MACOSX", "."))
    ]

    # 只有一个有效子目录，检查里面有没有 info.json
    if len(children) == 1 and (children[0] / INFO_JSON).is_file():
        return children[0]

    # 多个子目录时逐个查找
    for child in children:
        if (child / INFO_JSON).is_file():
            return child

    raise FileNotFoundError(
        f"zip 中未找到 {INFO_JSON}，请确保 zip 内包含 info.json 文件"
    )


def remove_identity(fingerprint_id: str) -> None:
    """删除 identity 对应的整个 user-data-dir。"""
    target = user_data_dir(fingerprint_id)
    if not target.exists():
        return
    try:
        shutil.rmtree(target)
    except Exception:
        logger.warning("[identity] 删除目录失败 %s", target, exc_info=True)
    logger.info("[identity] removed fingerprint_id=%s dir=%s", fingerprint_id, target)


def scan_identities(directory: Path | None = None) -> list[IdentityConfig]:
    """扫描指定目录（默认 ~/fp-data/）下的所有 identity 子目录。

    只收集包含合法 info.json 的子目录。
    """
    if directory is None:
        from core.constants import USER_DATA_DIR_PREFIX

        directory = Path.home() / USER_DATA_DIR_PREFIX

    if not directory.is_dir():
        return []

    result: list[IdentityConfig] = []
    for child in sorted(directory.iterdir()):
        if not child.is_dir():
            continue
        info_path = child / INFO_JSON
        if not info_path.is_file():
            continue
        try:
            identity = _read_info_json(child)
            result.append(identity)
        except Exception:
            logger.debug("[identity] 跳过无效目录 %s", child, exc_info=True)
    return result
