"""GET /api/mylist — マイリスト取得（F7・API設計書 B-11）。

going（行く予定）と favorites（お気に入り）を別セクションでまとめて返す。副作用なし。

第1段の確定仕様（API設計書 v1.3）:
- ① 楽さ `raku` は非表示（`store` に含めない・`lat/lng` 不要）。
- ② `going` は 1 店舗 1 エントリ。going_list の再タップは新規行のまま、
     表示時に `DISTINCT ON (store_id)` で最新 1 件へ畳む。favorites は
     `(user_id, store_id)` 一意で元来 1 店舗 1 件。
- ③ 掲載フィルタ（`is_listed`／`status`）はかけない＝非掲載・閉店疑いも表示。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text

from app.db import get_engine
from app.deps import get_current_uid

router = APIRouter()

# store（StoreItem 第1段）に載せる静的属性。raku・lat/lng は含めない（①）。
_STORE_COLS = ("store_id", "name", "category_l", "category_s", "area_label", "gmaps_url")


def _store(row: dict) -> dict:
    return {k: row[k] for k in _STORE_COLS}


@router.get("/api/mylist")
def get_mylist(uid: int = Depends(get_current_uid)):
    with get_engine().begin() as conn:
        # going：再タップ新規行を store_id 単位で最新 1 件に畳む（②）。
        # 掲載フィルタはかけない（③）。
        going_rows = conn.execute(
            text(
                """
                SELECT going_id, tapped_at, arrival_status,
                       store_id, name, category_l, category_s, area_label, gmaps_url
                FROM (
                    SELECT DISTINCT ON (g.store_id)
                           g.id AS going_id, g.tapped_at, g.arrival_status,
                           s.id AS store_id, s.name, s.category_l, s.category_s,
                           s.area_label, s.gmaps_url
                    FROM going_list g
                    JOIN stores s ON s.id = g.store_id
                    WHERE g.user_id = :uid
                    ORDER BY g.store_id, g.tapped_at DESC
                ) t
                ORDER BY t.tapped_at DESC
                """
            ),
            {"uid": uid},
        ).mappings().all()

        favorite_rows = conn.execute(
            text(
                """
                SELECT f.created_at,
                       s.id AS store_id, s.name, s.category_l, s.category_s,
                       s.area_label, s.gmaps_url
                FROM favorites f
                JOIN stores s ON s.id = f.store_id
                WHERE f.user_id = :uid
                ORDER BY f.created_at DESC
                """
            ),
            {"uid": uid},
        ).mappings().all()

    going = [
        {
            "going_id": int(r["going_id"]),
            "tapped_at": r["tapped_at"],
            "arrival_status": r["arrival_status"],
            "store": _store(r),
        }
        for r in going_rows
    ]
    favorites = [
        {
            "created_at": r["created_at"],
            "store": _store(r),
        }
        for r in favorite_rows
    ]
    return {"going": going, "favorites": favorites}
