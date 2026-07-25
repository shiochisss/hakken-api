"""store_photos テーブルへの書込。status='pending'・source='user' の行のみをINSERTする。

出典: hakken-f11 納品物（F11・おかむー）app/repositories/store_photos_repo.py を無改変で移植。
列構成は本番 schema_postgres.sql と一致することを確認済み。
※サムネイルのパスは保存しない（blob_path は本体のみ）。同一UUIDで
  stores/{store_id}/thumb/{uuid}.jpg として導出できる（app/storage/paths.py 参照）。
"""

from __future__ import annotations

from sqlalchemy import Connection, text


def insert_pending_user_photo(
    conn: Connection,
    *,
    store_id: int,
    blob_path: str,
    uploaded_by: int,
) -> int:
    row = conn.execute(
        text(
            """
            INSERT INTO store_photos
                (store_id, blob_path, status, source, uploaded_by, sort_order, is_primary,
                 created_at)
            VALUES
                (:store_id, :blob_path, 'pending', 'user', :uploaded_by, 0, false, now())
            RETURNING id
            """
        ),
        {"store_id": store_id, "blob_path": blob_path, "uploaded_by": uploaded_by},
    ).first()
    assert row is not None
    return int(row[0])
