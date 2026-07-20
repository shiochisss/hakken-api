"""バッチ共通：DB接続（SQLAlchemy engine）。

hakken-api/.env の DATABASE_URL を読む。本プロジェクトのドライバは psycopg（v3）
のため、plain な `postgresql://` スキームは `postgresql+psycopg://` に正規化する
（SQLAlchemy の既定 `postgresql://` は psycopg2 を要求するが未インストールのため）。
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

# hakken-api/.env を明示的に読む（実行ディレクトリに依存しない）
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _normalize_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL が未設定です（hakken-api/.env を確認）")
    return create_engine(_normalize_url(url), future=True)
