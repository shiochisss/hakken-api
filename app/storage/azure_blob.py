"""Azure Blob Storageへ実際に接続する本番向け実装。

接続方式は設定で切替可能:
  - AZURE_STORAGE_USE_MANAGED_IDENTITY=true → account_url + DefaultAzureCredential
  - それ以外（既定）                        → connection_string

- コンテナは非公開前提。公開アクセスは設定しない。
- SASは生成しない。Blob URLは返さない。
- Azure SDKの例外は BlobStorageError へ変換し、内部情報はログにのみ残す。

出典: hakken-f11 納品物（F11・おかむー）app/storage/azure_blob.py。
【移植時の変更】設定の参照方式のみ hakken-api 方式へ書き換えた。
  納品版: pydantic-settings の Settings オブジェクトをコンストラクタで受け取る
  本実装: app.config のモジュール定数を直接参照する（案a・pydantic-settings 不採用）
  ロジック（接続方式の分岐・コンテナ確認・upload/delete）は無変更。
"""

from __future__ import annotations

import logging

from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings

from app import config
from app.storage.base import BlobStorageError, BlobStoragePort

logger = logging.getLogger(__name__)


class AzureBlobStorage(BlobStoragePort):
    def __init__(self) -> None:
        self._container_name = config.AZURE_STORAGE_CONTAINER
        self._auto_create = config.AZURE_STORAGE_CONTAINER_AUTO_CREATE
        self._client = self._build_service_client()
        self._container_ready = False

    @staticmethod
    def _build_service_client() -> BlobServiceClient:
        if config.AZURE_STORAGE_USE_MANAGED_IDENTITY:
            if not config.AZURE_STORAGE_ACCOUNT_URL:
                raise BlobStorageError(
                    "AZURE_STORAGE_ACCOUNT_URL is required when using managed identity"
                )
            credential = DefaultAzureCredential()
            return BlobServiceClient(
                account_url=config.AZURE_STORAGE_ACCOUNT_URL, credential=credential
            )
        if not config.AZURE_STORAGE_CONNECTION_STRING:
            raise BlobStorageError(
                "AZURE_STORAGE_CONNECTION_STRING is required when not using managed identity"
            )
        return BlobServiceClient.from_connection_string(config.AZURE_STORAGE_CONNECTION_STRING)

    def _ensure_container(self) -> None:
        if self._container_ready:
            return
        container_client = self._client.get_container_client(self._container_name)
        if self._auto_create:
            try:
                # public_access は明示的に指定しない（非公開のまま作成）。
                container_client.create_container()
            except AzureError:
                # 既に存在する場合等はそのまま進める。
                pass
        else:
            if not container_client.exists():
                raise BlobStorageError(
                    f"container {self._container_name!r} does not exist and "
                    "auto-create is disabled; create it out-of-band or set "
                    "AZURE_STORAGE_CONTAINER_AUTO_CREATE=true"
                )
        self._container_ready = True

    def upload(self, blob_path: str, data: bytes, content_type: str) -> None:
        self._ensure_container()
        try:
            container_client = self._client.get_container_client(self._container_name)
            container_client.upload_blob(
                name=blob_path,
                data=data,
                overwrite=True,  # UUIDベースのパスのため衝突は想定しないが、意図を明示する
                content_settings=ContentSettings(content_type=content_type),
            )
        except AzureError as exc:
            logger.error("azure blob upload failed", extra={"blob_path": blob_path}, exc_info=exc)
            raise BlobStorageError("failed to upload blob") from exc

    def delete(self, blob_path: str) -> None:
        try:
            container_client = self._client.get_container_client(self._container_name)
            container_client.delete_blob(blob_path)
        except ResourceNotFoundError:
            return
        except AzureError as exc:
            logger.error("azure blob delete failed", extra={"blob_path": blob_path}, exc_info=exc)
            raise BlobStorageError("failed to delete blob") from exc
