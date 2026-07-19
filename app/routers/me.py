"""GET /api/me — ログイン中ユーザー取得（F1・API設計書 B-1）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text

from app.db import get_engine
from app.deps import get_current_uid

router = APIRouter()


@router.get("/api/me")
def get_me(uid: int = Depends(get_current_uid)):
    with get_engine().begin() as conn:
        row = conn.execute(
            text(
                """
                SELECT u.id, u.email,
                       EXISTS(SELECT 1 FROM user_conditions c WHERE c.user_id = u.id) AS has_conditions
                FROM users u
                WHERE u.id = :uid AND u.is_deleted = false
                """
            ),
            {"uid": uid},
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return {"id": int(row["id"]), "email": row["email"], "has_conditions": bool(row["has_conditions"])}
