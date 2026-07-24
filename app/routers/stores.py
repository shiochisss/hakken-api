"""GET /api/stores/{id} — 店詳細（F4・API設計書 B-7）。

単一店の詳細（現在地からの楽さ内訳込み）を返す。S3直リンク時に使用。
レスポンスは B-6 の items[] 要素と同形（StoreItem）。

設計書 B-7 の記載どおり:
  - パス: id（int）／クエリ: lat・lng（必須）。
  - レスポンス: StoreItem（B-6 items と同形。raku・boarding/alight・route_label・photo 含む）。
  - エラー: 401（未認証）／404（存在しない・非配信＝status≠営業中 or is_listed=false）。
  - 処理: 現在地からの乗車停→当該店の**最小 total 1行**を取得（reach 由来）。副作用なし。到達=walk1+ride+walk2（待ち時間捨象）。

※「半径R」について: B-7 は walk_max 等の条件を取らないため設計書に R の定義が無い（記載なし）。
  当該店の reach 行は少数のため、**候補を半径で絞らず全 boarding_stop から最小 total を選ぶ**
  （R は B-6 での候補絞り最適化であり、最小 total の正しさには不要）。
※lat/lng の範囲チェック(400)は B-7 の error 節に明記が無いが、B-6 と整合させて実装（下記コメント）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from app.db import get_engine
from app.deps import get_current_uid
# 徒歩式・座標妥当域は B-6 と同一のものを再利用（walk1 計算の一貫性を担保）
from app.routers.search import LAT_MAX, LAT_MIN, LNG_MAX, LNG_MIN, _haversine_m, _walk_min

router = APIRouter()


@router.get("/api/stores/{store_id}")
def get_store(
    store_id: int,
    lat: float = Query(...),
    lng: float = Query(...),
    uid: int = Depends(get_current_uid),
):
    # lat/lng 妥当域（B-6 と整合。B-7 error 節には明記なし＝整合目的で 400）
    if not (LAT_MIN <= lat <= LAT_MAX):
        raise HTTPException(status_code=400, detail="lat out of range")
    if not (LNG_MIN <= lng <= LNG_MAX):
        raise HTTPException(status_code=400, detail="lng out of range")

    with get_engine().begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT r.boarding_stop_id, r.ride_min, r.walk2_min, r.transfer, r.via_hub_id,
                       r.alight_stop_id, r.route_label,
                       s.name AS store_name, s.category_l, s.category_s, s.status,
                       s.address, s.area_label, s.lat AS store_lat, s.lng AS store_lng,
                       s.gmaps_url, s.hotpepper_url,
                       bs.name AS boarding_name, bs.lat AS b_lat, bs.lng AS b_lng,
                       als.name AS alight_name, hub.name AS hub_name
                FROM reach r
                JOIN stores s ON s.id = r.store_id
                JOIN stops bs ON bs.id = r.boarding_stop_id
                JOIN stops als ON als.id = r.alight_stop_id
                LEFT JOIN stops hub ON hub.id = r.via_hub_id
                WHERE r.store_id = :sid
                  AND s.status = '営業中' AND s.is_listed = true
                """
            ),
            {"sid": store_id},
        ).mappings().all()

        # 存在しない／非配信（status≠営業中 or is_listed=false）／到達不能 → 404（B-7）
        if not rows:
            raise HTTPException(status_code=404, detail="store not found or not available")

        # 現在地からの最小 total（同点は walk1 昇順）を全 boarding_stop から選ぶ
        best = None
        for r in rows:
            w1 = _walk_min(_haversine_m(lat, lng, r["b_lat"], r["b_lng"]))
            total = w1 + r["ride_min"] + r["walk2_min"]
            if best is None or (total, w1) < (best["total"], best["walk1"]):
                best = {"row": r, "walk1": w1, "total": total}

        # photo: hotpepper_url → store_photos(approved/is_primary) → none（SAS発行は#14で後日・ref=None）
        row = best["row"]
        hp = row["hotpepper_url"]
        if hp:
            photo = {"source": "hotpepper", "ref": hp}
        else:
            prow = conn.execute(
                text(
                    """
                    SELECT source FROM store_photos
                    WHERE store_id = :sid AND status = 'approved' AND is_primary = true
                    ORDER BY sort_order
                    LIMIT 1
                    """
                ),
                {"sid": store_id},
            ).mappings().first()
            photo = {"source": prow["source"], "ref": None} if prow else {"source": "none", "ref": None}

    return {
        "store_id": store_id,
        "name": row["store_name"],
        "category_l": row["category_l"],
        "category_s": row["category_s"],
        "status": row["status"],
        "photo": photo,
        "raku": {
            "walk1": best["walk1"], "ride": row["ride_min"], "walk2": row["walk2_min"],
            "total": best["total"], "transfer": row["transfer"],
            "via_hub": row["hub_name"] if row["transfer"] == "hub1" else None,
        },
        "boarding_stop": row["boarding_name"],
        "alight_stop": row["alight_name"],
        "route_label": row["route_label"],
        "address": row["address"],
        "area_label": row["area_label"],
        "lat": row["store_lat"],
        "lng": row["store_lng"],
        "gmaps_url": row["gmaps_url"],
    }
