"""
配置持久化：独立 SQLite 文件，存储 identity（浏览器状态快照）元数据。

表结构：identity（fingerprint_id, timezone, proxy_url, types, enabled, unfreeze_at）。
通过 identity_to_proxy_group 转换后向下游提供 ProxyGroupConfig。
"""

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from core.config.schema import (
    IdentityConfig,
    ProxyGroupConfig,
    identity_to_proxy_group,
)


DB_FILENAME = "db.sqlite3"
DB_PATH_ENV_KEY = "WEB2API_DB_PATH"


def _get_db_path() -> Path:
    configured = os.environ.get(DB_PATH_ENV_KEY, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parent.parent.parent / DB_FILENAME


def _init_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS identity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint_id TEXT NOT NULL UNIQUE,
            timezone TEXT NOT NULL DEFAULT '',
            proxy_url TEXT NOT NULL DEFAULT '',
            types TEXT NOT NULL DEFAULT '[]',
            enabled INTEGER NOT NULL DEFAULT 1,
            unfreeze_at TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()


class ConfigRepository:
    """Identity 配置的读写。"""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or _get_db_path()

    def _conn(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self._db_path)

    def init_schema(self) -> None:
        conn = self._conn()
        try:
            _init_tables(conn)
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Identity CRUD
    # -----------------------------------------------------------------------

    def save_identity(self, identity: IdentityConfig) -> None:
        """插入或更新一个 identity。"""
        conn = self._conn()
        try:
            _init_tables(conn)
            conn.execute(
                """
                INSERT INTO identity (fingerprint_id, timezone, proxy_url, types, enabled, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint_id) DO UPDATE SET
                    timezone = excluded.timezone,
                    proxy_url = excluded.proxy_url,
                    types = excluded.types,
                    enabled = excluded.enabled
                """,
                (
                    identity.fingerprint_id,
                    identity.timezone,
                    identity.proxy_url,
                    json.dumps(identity.types),
                    1 if identity.enabled else 0,
                    int(time.time()),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def delete_identity(self, fingerprint_id: str) -> None:
        conn = self._conn()
        try:
            _init_tables(conn)
            conn.execute(
                "DELETE FROM identity WHERE fingerprint_id = ?",
                (fingerprint_id,),
            )
            conn.commit()
        finally:
            conn.close()

    def set_identity_enabled(self, fingerprint_id: str, enabled: bool) -> None:
        conn = self._conn()
        try:
            _init_tables(conn)
            conn.execute(
                "UPDATE identity SET enabled = ? WHERE fingerprint_id = ?",
                (1 if enabled else 0, fingerprint_id),
            )
            conn.commit()
        finally:
            conn.close()

    def load_identities(self) -> list[IdentityConfig]:
        conn = self._conn()
        try:
            _init_tables(conn)
            rows = conn.execute(
                "SELECT fingerprint_id, timezone, proxy_url, types, enabled FROM identity ORDER BY id ASC"
            ).fetchall()
            result: list[IdentityConfig] = []
            for fingerprint_id, timezone, proxy_url, types_json, enabled in rows:
                try:
                    types = json.loads(types_json) if types_json else []
                except Exception:
                    types = []
                result.append(
                    IdentityConfig(
                        fingerprint_id=fingerprint_id,
                        timezone=timezone or "",
                        proxy_url=proxy_url or "",
                        types=types,
                        enabled=bool(enabled),
                    )
                )
            return result
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # unfreeze_at：按 (fingerprint_id, type) 存储解冻时间
    # -----------------------------------------------------------------------

    def update_account_unfreeze_at(
        self,
        fingerprint_id: str,
        type_name: str,
        unfreeze_at: int | None,
    ) -> None:
        """更新指定 identity 中某个 type 的解冻时间戳。

        unfreeze_at 以 JSON 字典存储：{"claude": 1234567890, ...}
        key 为 type_name，各 type 独立记录。
        """
        conn = self._conn()
        try:
            _init_tables(conn)
            row = conn.execute(
                "SELECT unfreeze_at FROM identity WHERE fingerprint_id = ?",
                (fingerprint_id,),
            ).fetchone()
            if row is None:
                return
            try:
                current = json.loads(row[0]) if row[0] else {}
            except Exception:
                current = {}
            if unfreeze_at is not None:
                current[type_name] = unfreeze_at
            else:
                current.pop(type_name, None)
            conn.execute(
                "UPDATE identity SET unfreeze_at = ? WHERE fingerprint_id = ?",
                (json.dumps(current), fingerprint_id),
            )
            conn.commit()
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # 向下游提供 ProxyGroupConfig（兼容现有调度层）
    # -----------------------------------------------------------------------

    def load_groups(self) -> list[ProxyGroupConfig]:
        """加载全部 identity 并转换为 ProxyGroupConfig 列表（含解冻时间）。"""
        conn = self._conn()
        try:
            _init_tables(conn)
            rows = conn.execute(
                "SELECT fingerprint_id, timezone, proxy_url, types, enabled, unfreeze_at "
                "FROM identity ORDER BY id ASC"
            ).fetchall()
            groups: list[ProxyGroupConfig] = []
            for fingerprint_id, timezone, proxy_url, types_json, enabled, unfreeze_json in rows:
                try:
                    types = json.loads(types_json) if types_json else []
                except Exception:
                    types = []
                try:
                    unfreeze_map = json.loads(unfreeze_json) if unfreeze_json else {}
                except Exception:
                    unfreeze_map = {}
                identity = IdentityConfig(
                    fingerprint_id=fingerprint_id,
                    timezone=timezone or "",
                    proxy_url=proxy_url or "",
                    types=types,
                    enabled=bool(enabled),
                )
                group = identity_to_proxy_group(identity)
                # 把 per-type 解冻时间注入到对应 AccountConfig
                if unfreeze_map:
                    from core.config.schema import AccountConfig

                    updated: list[AccountConfig] = []
                    for acc in group.accounts:
                        uf = unfreeze_map.get(acc.type)
                        if uf is not None:
                            acc = AccountConfig(
                                name=acc.name,
                                type=acc.type,
                                auth=acc.auth,
                                enabled=acc.enabled,
                                unfreeze_at=int(uf),
                            )
                        updated.append(acc)
                    group.accounts = updated
                groups.append(group)
            return groups
        finally:
            conn.close()

    def load_raw(self) -> list[dict[str, Any]]:
        """与前端 API 一致的原始列表格式（identity 视角）。"""
        identities = self.load_identities()
        return [
            {
                "fingerprint_id": i.fingerprint_id,
                "timezone": i.timezone,
                "proxy_url": i.proxy_url,
                "types": i.types,
                "enabled": i.enabled,
            }
            for i in identities
        ]
