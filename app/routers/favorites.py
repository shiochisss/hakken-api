"""POST/DELETE /api/favorites — お気に入り追加・解除（F6・API設計書 B-8/B-9）。

追加は重複を冪等に（DO NOTHING）→ 204。解除は未登録でも冪等に 204。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from starlette.responses import Response

from app.db import get_engine
from app.deps import get_current_uid

router = APIRouter()


class FavoriteIn(BaseModel):
    store_id: int


@router.post("/api/favorites")
def add_favorite(body: FavoriteIn, uid: int = Depends(get_current_uid)):
    with get_engine().begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM stores WHERE id = :sid"), {"sid": body.store_id}
        ).first()
        if exists is None:
            raise HTTPException(status_code=404, detail="store not found")
        # 重複は冪等（DO NOTHING）で 204（B-8 未決 → 204 を採用）
        conn.execute(
            text(
                """
                INSERT INTO favorites (user_id, store_id, created_at)
                VALUES (:uid, :sid, now())
                ON CONFLICT (user_id, store_id) DO NOTHING
                """
            ),
            {"uid": uid, "sid": body.store_id},
        )
    return Response(status_code=204)


@router.delete("/api/favorites/{store_id}")
def remove_favorite(store_id: int, uid: int = Depends(get_current_uid)):
    with get_engine().begin() as conn:
        # 未登録の解除も冪等に 204（404 にしない＝B-9）
        conn.execute(
            text("DELETE FROM favorites WHERE user_id = :uid AND store_id = :sid"),
            {"uid": uid, "sid": store_id},
        )
    return Response(status_code=204)
