"""stores テーブルへの読取専用アクセス。INSERT/UPDATE/DELETEは一切行わない。

出典: hakken-f11 納品物（F11・おかむー）app/repositories/stores_repo.py を無改変で移植。
"""

from __future__ import annotations

from sqlalchemy import Connection, text


def store_exists(conn: Connection, store_id: int) -> bool:
    row = conn.execute(text("SELECT 1 FROM stores WHERE id = :id"), {"id": store_id}).first()
    return row is not None
