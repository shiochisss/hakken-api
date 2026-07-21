"""POST /api/submissions — たれ込み投稿（F11・API設計書 B-15）。

stores には一切書かない（承認後に運営が手動で反映＝B-17）。写真投稿は B-16 が担当。
status='pending' で submissions に INSERT するのみ。

【暫定実装】このモジュールは仕様検証・叩き台のための暫定実装。F11（たれ込み投稿）は
おかむーさん（外部ベンダー）に実装を依頼済みで、納品後に本ファイルと差し替える予定。
認証契約（get_current_uid 経由で user_id を取得し submitted_by に格納）は技術回答書
どおりなので、差し替え時もこの契約を必ず維持すること。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from app.db import get_engine
from app.deps import get_current_uid

router = APIRouter()

_TYPES = {"new_store", "info_edit", "closure_report"}


class SubmissionIn(BaseModel):
    type: str
    store_id: int | None = None
    payload: dict


def _is_gmaps_url(url: str) -> bool:
    """Google マップ URL 形式のゆるい判定（B-15 節8）。"""
    return url.startswith("https://") and (
        "google.com/maps" in url
        or "maps.google.com" in url
        or "maps.app.goo.gl" in url
        or "goo.gl/maps" in url
    )


def validate_submission(type_: str, store_id: int | None, payload: dict) -> None:
    """B-15 節8 のドメイン検証。違反は ValueError（呼び出し側で 400）。"""
    if type_ not in _TYPES:
        raise ValueError("invalid type")
    if type_ == "new_store":
        if store_id is not None:
            raise ValueError("new_store must not have store_id")
        if not _is_gmaps_url(str(payload.get("gmaps_url", "")).strip()):
            raise ValueError("gmaps_url required (google maps url)")
    elif type_ == "info_edit":
        if store_id is None:
            raise ValueError("store_id required")
        if not str(payload.get("comment", "")).strip():
            raise ValueError("comment required")
    elif type_ == "closure_report":
        if store_id is None:
            raise ValueError("store_id required")
        if not str(payload.get("reason", "")).strip():
            raise ValueError("reason required")


@router.post("/api/submissions")
def create_submission(body: SubmissionIn, uid: int = Depends(get_current_uid)):
    try:
        validate_submission(body.type, body.store_id, body.payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    with get_engine().begin() as conn:
        # store_id 指定時（info_edit / closure_report）は存在チェック → 404
        if body.store_id is not None:
            exists = conn.execute(
                text("SELECT 1 FROM stores WHERE id = :sid"), {"sid": body.store_id}
            ).first()
            if exists is None:
                raise HTTPException(status_code=404, detail="store not found")
        row = conn.execute(
            text(
                """
                INSERT INTO submissions (type, store_id, payload, status, submitted_by, created_at)
                VALUES (:type, :sid, CAST(:payload AS JSONB), 'pending', :uid, now())
                RETURNING id
                """
            ),
            {"type": body.type, "sid": body.store_id, "payload": json.dumps(body.payload), "uid": uid},
        ).first()
    return {"submission_id": int(row[0])}
