"""認証依存：Cookie のセッションから user_id を解決する（F1）。

/api/* はこれを Depends して使う。未認証・期限切れ・退会（is_deleted）は 401。

依存は2段構え:
  - get_current_session … (uid, sid) を返す。**session_id も要る**エンドポイント用
    （検索の起点を sessions に記録する B-6/B-7、going_list に session_id を残す B-10）
  - get_current_uid     … 上の薄いラッパで uid だけ返す。従来どおりの用途
認証処理の本体は get_current_session の1箇所だけ（二重実装しない）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import NamedTuple

from fastapi import Depends, HTTPException, Request
from sqlalchemy import text

from app import config, security
from app.db import get_engine

_UNAUTH = HTTPException(status_code=401, detail="not authenticated")


class CurrentSession(NamedTuple):
    """ログイン中のユーザーIDと、そのアクセスに使われたセッションの行ID。"""

    uid: int
    sid: int


def get_current_session(request: Request) -> CurrentSession:
    token = request.cookies.get(config.SESSION_COOKIE_NAME)
    if not token:
        raise _UNAUTH
    token_hash = security.hash_token(token)
    with get_engine().begin() as conn:
        row = conn.execute(
            text(
                """
                SELECT s.id AS sid, s.user_id, s.expires_at, u.is_deleted
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = :th
                """
            ),
            {"th": token_hash},
        ).mappings().first()
        if row is None:
            raise _UNAUTH
        # 期限切れ・退会は無効化（行を掃除してから 401）
        if row["expires_at"] <= datetime.now(timezone.utc) or row["is_deleted"]:
            conn.execute(text("DELETE FROM sessions WHERE id = :sid"), {"sid": row["sid"]})
            raise _UNAUTH
        # 監査用に最終アクセス時刻を更新（軽量）
        conn.execute(
            text("UPDATE sessions SET last_seen_at = now() WHERE id = :sid"),
            {"sid": row["sid"]},
        )
        return CurrentSession(uid=int(row["user_id"]), sid=int(row["sid"]))


def get_current_uid(session: CurrentSession = Depends(get_current_session)) -> int:
    return session.uid
