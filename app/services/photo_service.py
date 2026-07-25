"""写真アップロードの一連の処理（検証→変換→Blob保存→DB INSERT）と補償処理。

補償ルール:
  - 本体保存失敗    → DB INSERTしない
  - サムネイル保存失敗 → 保存済み本体を削除しDB INSERTしない
  - DB INSERT/コミット失敗 → 本体・サムネイルとも削除

AzureBlobStorage / FakeBlobStorage のどちらでも、この同一クラスを利用できる
（コンストラクタが BlobStoragePort にのみ依存するため）。

出典: hakken-f11 納品物（F11・おかむー）app/services/photo_service.py を無改変で移植。
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import Engine

from app.imaging.convert import ImageDecodeError, process_image
from app.imaging.validate import UnsupportedImageError, validate_upload
from app.repositories.store_photos_repo import insert_pending_user_photo
from app.storage.base import BlobStorageError, BlobStoragePort
from app.storage.paths import build_photo_blob_paths

logger = logging.getLogger(__name__)


class PhotoProcessingError(ValueError):
    """検証・デコード段階の失敗。呼び出し側で400へ変換する。"""


class PhotoUploadFailedError(RuntimeError):
    """Blob保存・DB保存段階の失敗。呼び出し側で500へ変換する。"""


class PhotoUploadService:
    def __init__(self, storage: BlobStoragePort) -> None:
        self._storage = storage

    def upload(
        self,
        *,
        engine: Engine,
        raw_bytes: bytes,
        filename: str | None,
        content_type: str | None,
        store_id: int,
        uploaded_by: int,
    ) -> int:
        correlation_id = str(uuid.uuid4())

        try:
            kind = validate_upload(raw_bytes, filename=filename, content_type=content_type)
            processed = process_image(raw_bytes, kind)
        except (UnsupportedImageError, ImageDecodeError) as exc:
            raise PhotoProcessingError(str(exc)) from exc

        paths = build_photo_blob_paths(store_id)

        try:
            self._storage.upload(paths.main, processed.main_bytes, content_type="image/jpeg")
        except BlobStorageError as exc:
            logger.error(
                "main image upload failed",
                extra={
                    "correlation_id": correlation_id,
                    "blob_path": paths.main,
                    "stage": "main_upload",
                },
            )
            raise PhotoUploadFailedError("failed to store photo") from exc

        try:
            self._storage.upload(
                paths.thumbnail, processed.thumbnail_bytes, content_type="image/jpeg"
            )
        except BlobStorageError as exc:
            logger.error(
                "thumbnail upload failed",
                extra={
                    "correlation_id": correlation_id,
                    "blob_path": paths.thumbnail,
                    "stage": "thumbnail_upload",
                },
            )
            self._safe_delete(
                paths.main, correlation_id, stage="compensate_main_after_thumbnail_fail"
            )
            raise PhotoUploadFailedError("failed to store photo") from exc

        try:
            with engine.begin() as conn:
                photo_id = insert_pending_user_photo(
                    conn,
                    store_id=store_id,
                    blob_path=paths.main,
                    uploaded_by=uploaded_by,
                )
        except Exception as exc:
            logger.error(
                "db insert failed after blob upload",
                extra={"correlation_id": correlation_id, "stage": "db_insert"},
            )
            self._safe_delete(paths.main, correlation_id, stage="compensate_main_after_db_fail")
            self._safe_delete(
                paths.thumbnail, correlation_id, stage="compensate_thumbnail_after_db_fail"
            )
            raise PhotoUploadFailedError("failed to save photo record") from exc

        return photo_id

    def _safe_delete(self, blob_path: str, correlation_id: str, *, stage: str) -> None:
        try:
            self._storage.delete(blob_path)
        except BlobStorageError:
            # 元の例外は握りつぶさない：呼び出し元は別途 PhotoUploadFailedError を送出する。
            # ここでは補償削除自体の失敗を安全な情報のみでログに残す。
            logger.error(
                "compensating delete failed",
                extra={"correlation_id": correlation_id, "blob_path": blob_path, "stage": stage},
            )
