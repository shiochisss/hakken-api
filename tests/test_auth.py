"""F1 認証のテスト（DB・Google 非依存）。

- 純関数（token/hash/next 検証/id_token デコード）
- TestClient で「DBに触れない到達パス」（302/400/401）を確認
実行: (.venv 有効化後)  python -m tests.test_auth
"""
from __future__ import annotations

import base64
import json
import os

# app.config が import 時に env を読むため、先に設定する
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-secret")
os.environ.setdefault("FRONTEND_ORIGIN", "http://localhost:3000")
os.environ.setdefault("OAUTH_REDIRECT_URI", "http://localhost:8000/auth/google/callback")

from fastapi.testclient import TestClient  # noqa: E402

from app import config, security  # noqa: E402
from app.routers import auth  # noqa: E402
from main import app  # noqa: E402

client = TestClient(app)


def _fake_id_token(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"header.{body}.sig"


def test_hash_and_token():
    t = security.generate_token()
    assert isinstance(t, str) and len(t) >= 32
    assert security.generate_token() != security.generate_token()  # ランダム
    h = security.hash_token(t)
    assert len(h) == 64 and h == security.hash_token(t)  # 決定的・64桁


def test_valid_next():
    assert security.valid_next("http://localhost:3000/home") == "http://localhost:3000/home"
    assert security.valid_next(None) == config.FRONTEND_ORIGIN
    # 別オリジンは弾いて既定へ（オープンリダイレクト防止）
    assert security.valid_next("https://evil.example.com/x") == config.FRONTEND_ORIGIN


def test_decode_id_token():
    claims = auth._decode_id_token(_fake_id_token({"sub": "123", "email": "a@b.com"}))
    assert claims["sub"] == "123" and claims["email"] == "a@b.com"
    assert auth._decode_id_token("not-a-jwt") == {}


def test_login_redirects_to_google():
    r = client.get("/auth/google/login", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith(config.GOOGLE_AUTH_URL)
    # state Cookie が発行される
    assert config.STATE_COOKIE_NAME in r.headers.get("set-cookie", "")


def test_callback_rejects_bad_state():
    # state Cookie 無し → 400（DBに触れない）
    r = client.get("/auth/google/callback?code=x&state=y", follow_redirects=False)
    assert r.status_code == 400


def test_me_requires_auth():
    r = client.get("/api/me")
    assert r.status_code == 401


def test_logout_requires_cookie():
    r = client.post("/auth/logout")
    assert r.status_code == 401


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok: {t.__name__}")
    print(f"全 {len(tests)} テスト成功")


if __name__ == "__main__":
    main()
