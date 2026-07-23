"""バッチ共通：DB接続（SQLAlchemy engine）。

hakken-api/.env の DATABASE_URL を読む。本プロジェクトのドライバは psycopg（v3）
のため、plain な `postgresql://` スキームは `postgresql+psycopg://` に正規化する
（SQLAlchemy の既定 `postgresql://` は psycopg2 を要求するが未インストールのため）。
"""
from __future__ import annotations

import os
import re
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

# hakken-api/.env を明示的に読む（実行ディレクトリに依存しない）。
# override=False＝OSに既存の環境変数（$env:DATABASE_URL 等）を .env より優先する（本来の設計意図）。
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)


def _normalize_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def describe_target(url: str | None = None) -> str:
    """接続先を「host / db [本番|ローカル]」の1行で返す。認証情報（user/pass）は出さない。DBには接続しない。

    url 省略時は現在解決される DATABASE_URL（環境変数優先→.env）を使う。
    """
    if url is None:
        url = os.environ.get("DATABASE_URL")
    if not url:
        return "接続先: (DATABASE_URL 未設定)"
    p = urlsplit(url)
    host = p.hostname or "?"
    db = (p.path or "").lstrip("/") or "?"
    kind = "ローカル" if host in ("localhost", "127.0.0.1") else "本番"
    return f"接続先: {host} / {db} [{kind}]"


def log_connection_target() -> None:
    """バッチ起動時に接続先を標準出力へ表示（誤投入防止）。DBには接続しない。"""
    print(describe_target())


# 接続文字列のパスワード部を伏せる正規表現。
#   ①URL形式  ://user:pass@  → ://user:***@
#   ②key=value形式  password=xxx / pwd=xxx → password=***
_URL_PW_RE = re.compile(r"(://[^:/@\s]+:)[^@/\s]+(@)")
_KV_PW_RE = re.compile(r"((?:password|pwd)=)[^\s&;'\"]+", re.IGNORECASE)


def mask_secrets(text: object) -> str:
    """文字列（例外メッセージ等）に含まれる接続文字列のパスワードを *** に伏せて返す。

    例外・スタックトレースを表示/再送出する前に必ず通す。DBには接続しない。
    """
    s = str(text)
    s = _URL_PW_RE.sub(r"\1***\2", s)
    s = _KV_PW_RE.sub(r"\1***", s)
    return s


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL が未設定です（hakken-api/.env を確認）")
    try:
        return create_engine(_normalize_url(url), future=True)
    except Exception as e:  # noqa: BLE001 不正URL等でも接続文字列を露出させない
        raise RuntimeError(f"DBエンジン初期化に失敗: {describe_target(url)} :: {mask_secrets(e)}") from None


@contextmanager
def begin():
    """`get_engine().begin()` 相当のトランザクション。

    ★接続確立（認証・到達）に失敗した場合のみ、接続文字列のパスワードを伏せた
    メッセージに差し替えて再送出する（元例外の連鎖は from None で断ち、URLを含む
    可能性のあるトレースを外に出さない）。トランザクション本体（yield 内）の
    エラーはそのまま送出する（型・情報を保つ／本体エラーに認証情報は含まれない）。
    """
    engine = get_engine()
    try:
        conn = engine.connect()  # ← ここで実接続（認証）。失敗時はマスクして再送出
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"DB接続に失敗: {describe_target()} :: {mask_secrets(e)}") from None
    try:
        with conn.begin():
            yield conn
    finally:
        conn.close()
