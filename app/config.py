"""環境変数の読み込みと定数（F1 認証）。

秘密情報（GOOGLE_CLIENT_SECRET 等）は .env / Azure アプリ設定から読むのみ。
コード・ログに直書きしない。既定値はローカル開発向け（本番は Azure 側で上書き必須）。
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# hakken-api/.env を明示的に読む（実行ディレクトリに依存しない）
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# --- Google OAuth ---
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
# リダイレクトURI（Google Cloud に登録済みの値と完全一致させる）。
#   local = http://localhost:8000/auth/google/callback
#   prod  = https://hakken-bus-api-...azurewebsites.net/auth/google/callback（Azure アプリ設定で指定）
OAUTH_REDIRECT_URI = os.environ.get(
    "OAUTH_REDIRECT_URI", "http://localhost:8000/auth/google/callback"
)
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPE = "openid email"

# --- フロント連携（CORS / next 許可 / 既定リダイレクト先）---
FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "http://localhost:3000")

# --- セッション / Cookie ---
SESSION_COOKIE_NAME = "hakken_session"
STATE_COOKIE_NAME = "oauth_state"
NEXT_COOKIE_NAME = "oauth_next"
# 本番はクロスサイト（front=azurestaticapps / api=azurewebsites）のため None;Secure が必要。
# ローカルは同一サイト（localhost:3000↔8000）なので Lax + 非Secure で可。
SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
SESSION_COOKIE_SAMESITE = os.environ.get("SESSION_COOKIE_SAMESITE", "lax").lower()
SESSION_TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", "30"))


def session_max_age_seconds() -> int:
    return SESSION_TTL_DAYS * 24 * 60 * 60
