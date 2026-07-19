"""F1 Google 認証エンドポイント。

  GET  /auth/google/login     … Google 認可画面へ 302（state Cookie で CSRF 対策）
  GET  /auth/google/callback  … code 交換→users upsert→sessions 発行→Cookie セット→next へ 302
  POST /auth/logout           … セッション破棄→204

方式は Authorization Code フロー（API設計書 v1.2 A-2/B-2/B-3）。
id_token は Google の token エンドポイントから TLS で直接取得するため、payload の
デコードのみで sub/email を読む（署名再検証は OIDC 3.1.3.7 により不要）。
"""
from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import RedirectResponse, Response
from sqlalchemy import text

from app import config, security
from app.db import get_engine

router = APIRouter()


def _decode_id_token(id_token: str) -> dict:
    """JWT の payload（2番目のセグメント）を base64url デコードして claims を返す。"""
    try:
        payload_b64 = id_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # padding
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except (IndexError, ValueError, binascii.Error):
        return {}


@router.get("/auth/google/login")
def google_login(request: Request, next: str | None = None):
    if not config.GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="OAuth is not configured")
    target_next = security.valid_next(next)
    state = security.generate_token()
    params = {
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": config.OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": config.GOOGLE_SCOPE,
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    resp = RedirectResponse(f"{config.GOOGLE_AUTH_URL}?{urlencode(params)}", status_code=302)
    security.set_oauth_cookie(resp, config.STATE_COOKIE_NAME, state)
    security.set_oauth_cookie(resp, config.NEXT_COOKIE_NAME, target_next)
    return resp


@router.get("/auth/google/callback")
def google_callback(request: Request, code: str | None = None, state: str | None = None):
    state_cookie = request.cookies.get(config.STATE_COOKIE_NAME)
    next_cookie = request.cookies.get(config.NEXT_COOKIE_NAME)
    # CSRF：state クエリと state Cookie の一致を必須にする
    if not code or not state or not state_cookie or not secrets_equal(state, state_cookie):
        raise HTTPException(status_code=400, detail="invalid oauth state")

    # code → token 交換（Google と TLS で直接）
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.post(
                config.GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": config.GOOGLE_CLIENT_ID,
                    "client_secret": config.GOOGLE_CLIENT_SECRET,
                    "redirect_uri": config.OAUTH_REDIRECT_URI,
                    "grant_type": "authorization_code",
                },
            )
            r.raise_for_status()
            tok = r.json()
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="token exchange failed")

    claims = _decode_id_token(tok.get("id_token", ""))
    sub = claims.get("sub")
    email = claims.get("email")
    if not sub or not email:
        raise HTTPException(status_code=502, detail="invalid id_token")

    token = security.generate_token()
    token_hash = security.hash_token(token)
    expires_at = datetime.now(timezone.utc) + timedelta(days=config.SESSION_TTL_DAYS)

    with get_engine().begin() as conn:
        # 初回ログインなら作成、既存はそのまま（google_sub 一意）
        conn.execute(
            text(
                """
                INSERT INTO users (google_sub, email, created_at, is_deleted)
                VALUES (:sub, :email, now(), false)
                ON CONFLICT (google_sub) DO NOTHING
                """
            ),
            {"sub": sub, "email": email},
        )
        row = conn.execute(
            text("SELECT id, is_deleted FROM users WHERE google_sub = :sub"),
            {"sub": sub},
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=500, detail="user lookup failed")

        # 退会ユーザーは復活させない（方針A）。セッションを発行せず S0 へ戻す。
        if row["is_deleted"]:
            resp = RedirectResponse(config.FRONTEND_ORIGIN, status_code=302)
            security.clear_oauth_cookies(resp)
            return resp

        conn.execute(
            text(
                """
                INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen_at)
                VALUES (:th, :uid, now(), :exp, now())
                """
            ),
            {"th": token_hash, "uid": int(row["id"]), "exp": expires_at},
        )

    resp = RedirectResponse(security.valid_next(next_cookie), status_code=302)
    security.set_session_cookie(resp, token)
    security.clear_oauth_cookies(resp)
    return resp


@router.post("/auth/logout")
def logout(request: Request):
    token = request.cookies.get(config.SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="not authenticated")
    with get_engine().begin() as conn:
        conn.execute(
            text("DELETE FROM sessions WHERE token_hash = :th"),
            {"th": security.hash_token(token)},
        )
    resp = Response(status_code=204)
    security.clear_session_cookie(resp)
    return resp


def secrets_equal(a: str, b: str) -> bool:
    """タイミング攻撃を避ける定数時間比較。"""
    import hmac

    return hmac.compare_digest(a, b)
