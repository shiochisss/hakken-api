"""POST /api/submissions — たれ込み投稿（F11・API設計書 B-15）。

storesには一切書き込まない（承認後に運営が手動で反映＝B-17）。写真投稿は B-16 が担当。
submissionsへ status='pending' のINSERTのみ行う。

store_id は「型/入力形式が不正→400」「数値だが stores.id に存在しない→404」で統一する
（RFP/技術回答の一部記載との差異は hakken-f11/docs/spec_conflicts.md 矛盾6 参照）。

出典: hakken-f11 納品物（F11・おかむー）app/routers/submissions.py を無改変で移植し、
旧・暫定実装（叩き台）を置き換えたもの。認証契約（get_current_uid 経由で user_id を
取得し submitted_by に格納）は旧実装から維持している。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Engine

from app.db import get_engine
from app.deps import get_current_uid
from app.repositories.stores_repo import store_exists
from app.repositories.submissions_repo import insert_submission
from app.schemas.submission import SubmissionIn, SubmissionOut
from app.services.rate_limit import check_submissions_rate_limit

router = APIRouter()


@router.post("/api/submissions", response_model=SubmissionOut)
def create_submission(
    body: SubmissionIn,
    uid: int = Depends(get_current_uid),
    engine: Engine = Depends(get_engine),
) -> SubmissionOut:
    with engine.begin() as conn:
        # 連投レート制限（API設計書 A-10）。INSERTより前に判定して打ち切る。
        if not check_submissions_rate_limit(conn, uid):
            raise HTTPException(status_code=429, detail="too many submissions")

        if body.store_id is not None and not store_exists(conn, body.store_id):
            raise HTTPException(status_code=404, detail="store_id not found")

        submission_id = insert_submission(
            conn,
            type_=body.type,
            store_id=body.store_id,
            payload=body.payload,
            submitted_by=uid,
        )

    return SubmissionOut(submission_id=submission_id)
