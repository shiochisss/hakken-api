"""POST /api/submissions/photo-upload のレスポンススキーマ。

レスポンスには blob_path・Blob URL・SAS URL・コンテナ名・元ファイル名・
Storage Account情報を一切含めない。

出典: hakken-f11 納品物（F11・おかむー）app/schemas/photo.py を無改変で移植。
"""

from __future__ import annotations

from pydantic import BaseModel


class PhotoUploadOut(BaseModel):
    photo_id: int
