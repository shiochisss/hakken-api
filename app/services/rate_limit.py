"""レート制限（API設計書 A-10・v1.7）。

`RateLimiter` Protocol・`FakeRateLimiter` は元々インターフェースのみが定義され、本番用
インメモリ実装は意図的に組み込まれていなかった（複数インスタンスでカウンターが共有され
ず本番要件を満たさないため）。出典: hakken-f11 納品物（F11・おかむー）を無改変で移植。

v1.7 でその本番実装として **DBカウント方式** を追加する（下記 check_submissions_rate_limit /
check_photo_upload_rate_limit）。新規テーブル・新規外部サービス（Redis等）は使わず、
`submissions`/`store_photos` の既存カラム（`created_at`）を直近ウィンドウでCOUNTする。
本番は `gunicorn -w 2`（複数ワーカープロセス）で稼働しており、上記の理由でインメモリの
`RateLimiter` 実装は使えないが、DB（PostgreSQL）は全ワーカー・全インスタンスから共有される
単一の実データ源のため、追加インフラなしに解決できる。

同時多発リクエストによる多少のカウント漏れ（レース）は許容する（他の暫定値と同じ扱い。
厳密な排他制御はしない）。

【2026-08-14 判断】`submissions(submitted_by, created_at)` / `store_photos(uploaded_by,
created_at)` への複合インデックスは今回追加しない。MVP規模（掲載16店・低トラフィック、
B-15/B-16はユーザーが能動的に投稿するときだけ呼ばれる低頻度操作）ではインデックス無しの
シーケンシャルスキャンでも実害が無いため、後から投稿数が増えたタイミングで追加する方針
（hakken-docs CLAUDE.md 7章にも記載）。
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy import text
from sqlalchemy.engine import Connection


class RateLimiter(Protocol):
    def check(self, key: str) -> bool:
        """呼び出し可能ならTrue、レート制限に達しているならFalseを返す。"""
        ...


class FakeRateLimiter:
    """テスト専用。常に許可/常に拒否を明示的に切り替えられる。"""

    def __init__(self, *, allow: bool = True) -> None:
        self.allow = allow
        self.calls: list[str] = []

    def check(self, key: str) -> bool:
        self.calls.append(key)
        return self.allow


# --- B-15/B-16 本番実装（DBカウント方式）。値は暫定・標準プロファイル（運用しながら調整）。---

SUBMISSIONS_WINDOW_MINUTES = 10   # B-15: この分数あたり
SUBMISSIONS_LIMIT = 5             # ...この件数まで（type問わず合算）

PHOTO_HOURLY_LIMIT = 10           # B-16: 1時間あたりこの枚数まで
PHOTO_DAILY_LIMIT = 30            # B-16: 1日あたりこの枚数まで（両方を満たす必要がある）


def check_submissions_rate_limit(conn: Connection, uid: int) -> bool:
    """直近 SUBMISSIONS_WINDOW_MINUTES 分の投稿数が SUBMISSIONS_LIMIT 未満なら True（B-15）。"""
    row = conn.execute(
        text(
            f"""
            SELECT COUNT(*) AS n FROM submissions
            WHERE submitted_by = :uid
              AND created_at >= now() - interval '{SUBMISSIONS_WINDOW_MINUTES} minutes'
            """
        ),
        {"uid": uid},
    ).mappings().first()
    return int(row["n"]) < SUBMISSIONS_LIMIT


def check_photo_upload_rate_limit(conn: Connection, uid: int) -> bool:
    """1時間 PHOTO_HOURLY_LIMIT 枚 かつ 1日 PHOTO_DAILY_LIMIT 枚。両方を満たせば True（B-16）。

    Blob保存・画像処理（EXIF除去・リサイズ）より前に呼び、超過分の処理コスト・
    Azure Blobの書き込み課金を発生させない（router側の責務）。
    FILTER 句で1クエリにまとめる（アップロード都度のDB往復を1回に抑える）。
    """
    row = conn.execute(
        text(
            """
            SELECT
                COUNT(*) FILTER (WHERE created_at >= now() - interval '1 hour') AS hourly,
                COUNT(*) FILTER (WHERE created_at >= now() - interval '1 day')  AS daily
            FROM store_photos
            WHERE uploaded_by = :uid
              AND created_at >= now() - interval '1 day'
            """
        ),
        {"uid": uid},
    ).mappings().first()
    return int(row["hourly"]) < PHOTO_HOURLY_LIMIT and int(row["daily"]) < PHOTO_DAILY_LIMIT
