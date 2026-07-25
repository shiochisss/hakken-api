"""自動テスト専用の疑似Storage実装。Azureへは一切接続しない。

本番コードパスからは選択されない。差し替えは pytest 等から
`app.dependency_overrides[get_blob_storage] = lambda: fake_storage` でのみ行う。

出典: hakken-f11 納品物（F11・おかむー）app/storage/fake_blob.py を無改変で移植。
【移植時の変更】実装本体に含めないため app/storage/ ではなく tests/ 配下へ配置した。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.storage.base import BlobStorageError, BlobStoragePort


@dataclass
class UploadRecord:
    blob_path: str
    data: bytes
    content_type: str


class FakeBlobStorage(BlobStoragePort):
    def __init__(self) -> None:
        self._data: dict[str, UploadRecord] = {}
        self.upload_history: list[UploadRecord] = []
        self.delete_history: list[str] = []
        self._fail_on_upload: set[str] = set()
        self._fail_on_delete: set[str] = set()

    # --- テスト制御用API ---
    def fail_next_upload_for(self, blob_path: str) -> None:
        self._fail_on_upload.add(blob_path)

    def fail_next_delete_for(self, blob_path: str) -> None:
        self._fail_on_delete.add(blob_path)

    def exists(self, blob_path: str) -> bool:
        return blob_path in self._data

    def get(self, blob_path: str) -> UploadRecord | None:
        return self._data.get(blob_path)

    # --- BlobStoragePort実装 ---
    def upload(self, blob_path: str, data: bytes, content_type: str) -> None:
        if blob_path in self._fail_on_upload:
            self._fail_on_upload.discard(blob_path)
            raise BlobStorageError(f"forced upload failure for {blob_path!r}")
        record = UploadRecord(blob_path=blob_path, data=data, content_type=content_type)
        self._data[blob_path] = record
        self.upload_history.append(record)

    def delete(self, blob_path: str) -> None:
        if blob_path in self._fail_on_delete:
            self._fail_on_delete.discard(blob_path)
            raise BlobStorageError(f"forced delete failure for {blob_path!r}")
        self._data.pop(blob_path, None)
        self.delete_history.append(blob_path)
