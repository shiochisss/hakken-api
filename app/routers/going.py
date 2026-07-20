"""POST /api/going — ここ行く（F7・API設計書 B-10）。

going_list に新規行を作り、同一トランザクションで event_log に koko_iku を追記する
（koko_iku の記録主体はサーバに確定＝2026-07-15）。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from app.db import get_engine
from app.deps import get_current_uid

router = APIRouter()


class GoingMeta(BaseModel):
    raku: dict | None = None


class GoingIn(BaseModel):
    store_id: int
    meta: GoingMeta | None = None


@router.post("/api/going")
def create_going(body: GoingIn, uid: int = Depends(get_current_uid)):
    raku = body.meta.raku if body.meta else None
    raku_json = json.dumps(raku) if raku is not None else None
    with get_engine().begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM stores WHERE id = :sid"), {"sid": body.store_id}
        ).first()
        if exists is None:
            raise HTTPException(status_code=404, detail="store not found")
        # 再タップは新規行（B-10 仕様）
        row = conn.execute(
            text(
                """
                INSERT INTO going_list (user_id, store_id, tapped_at, arrival_status)
                VALUES (:uid, :sid, now(), 'none')
                RETURNING id
                """
            ),
            {"uid": uid, "sid": body.store_id},
        ).first()
        going_id = int(row[0])
        conn.execute(
            text(
                """
                INSERT INTO event_log (user_id, ts, event_type, store_id, meta_json)
                VALUES (:uid, now(), 'koko_iku', :sid, CAST(:meta AS JSONB))
                """
            ),
            {"uid": uid, "sid": body.store_id, "meta": raku_json},
        )
    return {"going_id": going_id}
