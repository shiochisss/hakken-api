"""POST /api/events — 計測イベント追記（API設計書 B-14）。

event_log への追記のみ（更新・削除しない）。ベストエフォートだが、不正 event_type は
DB の CHECK 制約に触れて 500 になるのを避けるため 400 で軽く弾く。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from starlette.responses import Response

from app.db import get_engine
from app.deps import get_current_uid

router = APIRouter()

# DB の CHECK と一致（サーバ記録の koko_iku/arrived_* も含む 8 種）
_EVENT_TYPES = {
    "app_open", "list_shown", "store_view", "favorite",
    "koko_iku", "gmaps_out", "arrived_pending", "arrived_verified",
}


class EventIn(BaseModel):
    event_type: str
    store_id: int | None = None
    meta: dict | None = None


@router.post("/api/events")
def add_event(body: EventIn, uid: int = Depends(get_current_uid)):
    if body.event_type not in _EVENT_TYPES:
        raise HTTPException(status_code=400, detail="invalid event_type")
    meta_json = json.dumps(body.meta) if body.meta is not None else None
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO event_log (user_id, ts, event_type, store_id, meta_json)
                VALUES (:uid, now(), :et, :sid, CAST(:meta AS JSONB))
                """
            ),
            {"uid": uid, "et": body.event_type, "sid": body.store_id, "meta": meta_json},
        )
    return Response(status_code=204)
