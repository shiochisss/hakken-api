"""Storage層の抽象インターフェース。RouterやServiceはこれにのみ依存し、
Azure SDK固有の型を持ち込まない。公開URL・SAS URLを返す設計にしないこと。

出典: hakken-f11 納品物（F11・おかむー）app/storage/base.py を無改変で移植。
"""

from __future__ import annotations

from typing import Protocol


class BlobStorageError(Exception):
    """Storage操作の失敗を表すアプリケーション例外。Azure SDK固有の例外はここへ変換する。"""


class BlobStoragePort(Protocol):
    def upload(self, blob_path: str, data: bytes, content_type: str) -> None:
        """指定パスへデータを保存する。公開URL/SAS URLは返さない。"""
        ...

    def delete(self, blob_path: str) -> None:
        """指定パスのBlobを削除する。存在しない場合も例外にしない実装を推奨。"""
        ...
