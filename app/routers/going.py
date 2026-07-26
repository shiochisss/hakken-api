"""POST /api/going — ここ行く（F7・API設計書 B-10）。

going_list に新規行を作り、同一トランザクションで event_log に koko_iku を追記する
（koko_iku の記録主体はサーバに確定＝2026-07-15）。

2026-07-27 追加: 「その提案はどこ起点だったか」を後から辿れるように、宣言したセッションの
id と**宣言時点の起点**（`sessions.origin_*` からの転記）を going_list に残す。
リクエストの契約は変えない（フロントは無変更）＝起点はサーバが持っている値を使う。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from app.db import get_engine
from app.deps import CurrentSession, get_current_session

router = APIRouter()


class GoingMeta(BaseModel):
    raku: dict | None = None


class GoingIn(BaseModel):
    store_id: int
    meta: GoingMeta | None = None


@router.post("/api/going")
def create_going(body: GoingIn, session: CurrentSession = Depends(get_current_session)):
    uid = session.uid
    raku = body.meta.raku if body.meta else None
    raku_json = json.dumps(raku) if raku is not None else None
    with get_engine().begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM stores WHERE id = :sid"), {"sid": body.store_id}
        ).first()
        if exists is None:
            raise HTTPException(status_code=404, detail="store not found")
        # 再タップは新規行（B-10 仕様）
        # 起点はセッションから転記する（直前の検索／店詳細が入れた値）。sessions は
        # ログアウト・期限切れで消えるうえ「そのセッションで最後に検索した場所」で
        # 上書きされ続けるため、宣言時点の値をここでコピーして固定する。
        # 検索を一度も通っていないセッション（理論上のみ）では origin_* が NULL になる＝許容。
        row = conn.execute(
            text(
                """
                INSERT INTO going_list (user_id, store_id, tapped_at, arrival_status,
                                        session_id, origin_lat, origin_lng, origin_label)
                SELECT :uid, :sid, now(), 'none',
                       s.id, s.origin_lat, s.origin_lng, s.origin_label
                FROM sessions s
                WHERE s.id = :session_id
                RETURNING id
                """
            ),
            {"uid": uid, "sid": body.store_id, "session_id": session.sid},
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
