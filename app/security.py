"""トークン生成・ハッシュ・Cookie ヘルパ（F1 認証）。"""
from __future__ import annotations

import hashlib
import secrets
from urllib.parse import urlsplit

from starlette.responses import Response

from app import config


def generate_token() -> str:
    """256bit 相当のランダムなセッショントークン（生値。Cookie にのみ載せる）。"""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """DB 保存用の SHA-256 hex（64桁）。生値は保存しない。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def valid_next(next_url: str | None) -> str:
    """オープンリダイレクト防止：FRONTEND_ORIGIN と同一 origin のみ許可。不正は既定へ。"""
    if not next_url:
        return config.FRONTEND_ORIGIN
    s = urlsplit(next_url)
    f = urlsplit(config.FRONTEND_ORIGIN)
    if s.scheme == f.scheme and s.netloc == f.netloc:
        return next_url
    return config.FRONTEND_ORIGIN


def set_session_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(
        key=config.SESSION_COOKIE_NAME,
        value=token,
        max_age=config.session_max_age_seconds(),
        httponly=True,
        secure=config.SESSION_COOKIE_SECURE,
        samesite=config.SESSION_COOKIE_SAMESITE,
        path="/",
    )


def clear_session_cookie(resp: Response) -> None:
    resp.delete_cookie(
        key=config.SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=config.SESSION_COOKIE_SECURE,
        samesite=config.SESSION_COOKIE_SAMESITE,
    )


def set_oauth_cookie(resp: Response, name: str, value: str) -> None:
    """OAuth ハンドシェイク用の短命 Cookie（state / next）。path は /auth に限定。"""
    resp.set_cookie(
        key=name,
        value=value,
        max_age=600,  # 10分
        httponly=True,
        secure=config.SESSION_COOKIE_SECURE,
        samesite=config.SESSION_COOKIE_SAMESITE,
        path="/auth",
    )


def clear_oauth_cookies(resp: Response) -> None:
    for name in (config.STATE_COOKIE_NAME, config.NEXT_COOKIE_NAME):
        resp.delete_cookie(
            key=name,
            path="/auth",
            httponly=True,
            secure=config.SESSION_COOKIE_SECURE,
            samesite=config.SESSION_COOKIE_SAMESITE,
        )
