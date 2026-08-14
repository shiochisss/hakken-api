"""POST /api/submissions/photo-upload — 写真アップロード（F11・API設計書 B-16）。

storesには一切書き込まない。検証・画像処理・Blob保存・store_photosへのpending行
INSERTはすべて app.services.photo_service.PhotoUploadService に委譲する。

出典: hakken-f11 納品物（F11・おかむー）app/routers/photo_upload.py を無改変で移植。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import Engine

from app.db import get_engine
from app.deps import get_current_uid
from app.imaging.constants import MAX_UPLOAD_BYTES, READ_CHUNK_BYTES
from app.repositories.stores_repo import store_exists
from app.schemas.photo import PhotoUploadOut
from app.services.photo_service import (
    PhotoProcessingError,
    PhotoUploadFailedError,
    PhotoUploadService,
)
from app.services.rate_limit import check_photo_upload_rate_limit
from app.storage.base import BlobStoragePort
from app.storage.dependency import get_blob_storage

router = APIRouter()


async def _read_with_limit(file: UploadFile, max_bytes: int) -> bytes:
    """Content-Lengthを信用せず、チャンク単位で読みながら実バイト数で上限判定する。
    上限を超えた時点で読み込みを打ち切り、413を送出する。
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail="file exceeds maximum allowed size")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/api/submissions/photo-upload", response_model=PhotoUploadOut)
async def upload_photo(
    store_id: int = Form(...),
    file: UploadFile = File(...),
    uid: int = Depends(get_current_uid),
    engine: Engine = Depends(get_engine),
    storage: BlobStoragePort = Depends(get_blob_storage),
) -> PhotoUploadOut:
    # store_id は数値として正常だが存在しない場合のみ404（型/形式不正はForm(...)の
    # 自動バリデーションにより422→main.pyのハンドラで400になる）。
    # 連投レート制限（API設計書 A-10）を最初に判定する。ファイル読み込み・画像処理
    # （EXIF除去・リサイズ）・Blob書き込みより前に打ち切ることで、超過分の処理コスト・
    # Azure Blobの課金を発生させない。
    with engine.begin() as conn:
        if not check_photo_upload_rate_limit(conn, uid):
            raise HTTPException(status_code=429, detail="too many photo uploads")
        if not store_exists(conn, store_id):
            raise HTTPException(status_code=404, detail="store_id not found")

    raw_bytes = await _read_with_limit(file, MAX_UPLOAD_BYTES)

    service = PhotoUploadService(storage)
    try:
        photo_id = service.upload(
            engine=engine,
            raw_bytes=raw_bytes,
            filename=file.filename,
            content_type=file.content_type,
            store_id=store_id,
            uploaded_by=uid,
        )
    except PhotoProcessingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PhotoUploadFailedError as exc:
        raise HTTPException(status_code=500, detail="failed to process photo upload") from exc

    return PhotoUploadOut(photo_id=photo_id)
