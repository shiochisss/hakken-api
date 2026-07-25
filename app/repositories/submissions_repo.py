"""submissions テーブルへの書込。status='pending' の行のみをINSERTする。

出典: hakken-f11 納品物（F11・おかむー）app/repositories/submissions_repo.py を無改変で移植。
列構成は本番 schema_postgres.sql と一致することを確認済み。
"""

from __future__ import annotations

import json

from sqlalchemy import Connection, text


def insert_submission(
    conn: Connection,
    *,
    type_: str,
    store_id: int | None,
    payload: dict,
    submitted_by: int,
) -> int:
    row = conn.execute(
        text(
            """
            INSERT INTO submissions (type, store_id, payload, status, submitted_by, created_at)
            VALUES (:type, :store_id, CAST(:payload AS JSONB), 'pending', :submitted_by, now())
            RETURNING id
            """
        ),
        {
            "type": type_,
            "store_id": store_id,
            "payload": json.dumps(payload),
            "submitted_by": submitted_by,
        },
    ).first()
    assert row is not None
    return int(row[0])
