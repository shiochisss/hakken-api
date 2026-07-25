"""Storage実装をFastAPI Dependency Injectionで差し替え可能にする。

安全設計として、この関数は常に AzureBlobStorage を返す。FakeBlobStorage への切替は
テストの `app.dependency_overrides[get_blob_storage] = lambda: fake_storage` でのみ行い、
環境変数だけで本番がFakeへ切り替わることがないようにしている。

出典: hakken-f11 納品物（F11・おかむー）app/storage/dependency.py。
【移植時の変更】AzureBlobStorage が Settings を受け取らなくなったため引数を削除した。
"""

from __future__ import annotations

from functools import lru_cache

from app.storage.azure_blob import AzureBlobStorage
from app.storage.base import BlobStoragePort


@lru_cache
def _cached_azure_storage() -> AzureBlobStorage:
    return AzureBlobStorage()


def get_blob_storage() -> BlobStoragePort:
    return _cached_azure_storage()
