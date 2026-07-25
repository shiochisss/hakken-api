"""Blobパスの生成を1箇所に集約する。

現時点では以下を暫定初期値として使用する（確定仕様ではない。命名規則が確定したら
このファイルのみを変更すればよい）。

    本体　　: stores/{store_id}/{uuid}.jpg
    サムネイル: stores/{store_id}/thumb/{uuid}.jpg

本体とサムネイルは同じUUIDを使用し、UUIDはサーバ側でuuid4により生成する。
元ファイル名・個人情報は一切パスに含めない。
※store_photos.blob_path に保存されるのは本体のみ。サムネイルは同一UUIDから導出する。

出典: hakken-f11 納品物（F11・おかむー）app/storage/paths.py を無改変で移植。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class PhotoBlobPaths:
    main: str
    thumbnail: str


def build_photo_blob_paths(store_id: int, *, photo_uuid: str | None = None) -> PhotoBlobPaths:
    token = photo_uuid or str(uuid.uuid4())
    return PhotoBlobPaths(
        main=f"stores/{store_id}/{token}.jpg",
        thumbnail=f"stores/{store_id}/thumb/{token}.jpg",
    )
