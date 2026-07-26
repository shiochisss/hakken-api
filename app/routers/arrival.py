"""F8「着いたよ」・F8b「着いたバナー」（API設計書 B-12 / B-13）。

  B-12 POST /api/going/{going_id}/arrived … 来訪の自己申告＋前面GPS照合
  B-13 GET  /api/arrival-banner           … 「行く予定×48h以内×150m以内×最近傍1件」

判定は**サーバ側**で行い、**位置の生値は保存しない**（照合結果だけを going_list に残す）。
計測（arrived_verified / arrived_pending）の記録主体もサーバ＝ B-10 の koko_iku と同じ流儀で、
フロントは `logEvent` を呼ばない（二重計上を避けるため）。

判定の距離は search.py の `_haversine_m` を再利用する。検索の徒歩判定と同じ球モデルで
揃えておかないと、「検索では徒歩1分と出たのにバナーが出ない」といった食い違いが起きる。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text

from app.db import get_engine
from app.deps import get_current_uid
# 距離式・座標妥当域は B-6 と同一のものを再利用（stores.py と同じ流儀）
from app.routers.search import LAT_MAX, LAT_MIN, LNG_MAX, LNG_MIN, _haversine_m

router = APIRouter()

# ★★★ 照合半径・遡り時間は暫定（API設計書 B-12/B-13 の未決事項#10）★★★
#   実地で「店の中にいるのに pending になる／隣の店で verified になる」が出たら見直す。
ARRIVAL_RADIUS_M = 150      # これ以内なら verified、超えたら pending
BANNER_WINDOW_HOURS = 48    # 「ここ行く」からこの時間内の行だけバナー対象


class ArrivedIn(BaseModel):
    lat: float
    lng: float


def judge(distance_m: float) -> str:
    """距離から照合結果を決める（DB非依存・テスト対象）。

    境界（ちょうど ARRIVAL_RADIUS_M）は verified 側に含める。店の登録座標は
    建物の代表点でしかなく、境界ぴったりを落とす理由が無いため。
    """
    return "verified" if distance_m <= ARRIVAL_RADIUS_M else "pending"


def _validate_pos(lat: float, lng: float) -> None:
    """B-6 と同じ座標妥当域チェック（範囲外は 400）。"""
    if not (LAT_MIN <= lat <= LAT_MAX):
        raise HTTPException(status_code=400, detail="lat out of range")
    if not (LNG_MIN <= lng <= LNG_MAX):
        raise HTTPException(status_code=400, detail="lng out of range")


@router.post("/api/going/{going_id}/arrived")
def arrived(going_id: int, body: ArrivedIn, uid: int = Depends(get_current_uid)):
    _validate_pos(body.lat, body.lng)

    with get_engine().begin() as conn:
        # 所有チェックは SQL の WHERE に含める（他人の going_id なら 0件＝404）
        row = conn.execute(
            text(
                """
                SELECT g.store_id, s.lat, s.lng
                FROM going_list g
                JOIN stores s ON s.id = g.store_id
                WHERE g.id = :gid AND g.user_id = :uid
                """
            ),
            {"gid": going_id, "uid": uid},
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="going not found")

        result = judge(_haversine_m(body.lat, body.lng, row["lat"], row["lng"]))

        # 再POSTは許可する（pending→verified の再確定がマイリストの導線。
        # フロントは verified 以外でボタンを出す実装＝mylist/page.tsx）。
        conn.execute(
            text(
                """
                UPDATE going_list
                SET arrival_status = :result, arrived_at = now()
                WHERE id = :gid AND user_id = :uid
                """
            ),
            {"result": result, "gid": going_id, "uid": uid},
        )
        conn.execute(
            text(
                """
                INSERT INTO event_log (user_id, ts, event_type, store_id, meta_json)
                VALUES (:uid, now(), :etype, :sid, NULL)
                """
            ),
            {"uid": uid, "etype": f"arrived_{result}", "sid": row["store_id"]},
        )

    return {"result": result}


@router.get("/api/arrival-banner")
def arrival_banner(
    lat: float = Query(...),
    lng: float = Query(...),
    uid: int = Depends(get_current_uid),
):
    _validate_pos(lat, lng)

    with get_engine().begin() as conn:
        # 対象は「自分の・未着・48h以内」の行だけ＝通常は数件。距離は Python 側で
        # _haversine_m を使う（PostGIS を入れておらず、検索と同じ球モデルで揃えたいため）。
        # pending は含めない＝早押しでバナーが出続けるのを避ける（B-13 の決定・2026-07-26）。
        rows = conn.execute(
            text(
                f"""
                SELECT g.id AS going_id, g.store_id, s.name AS store_name, s.lat, s.lng
                FROM going_list g
                JOIN stores s ON s.id = g.store_id
                WHERE g.user_id = :uid
                  AND g.arrival_status = 'none'
                  AND g.tapped_at >= now() - interval '{BANNER_WINDOW_HOURS} hours'
                """
            ),
            {"uid": uid},
        ).mappings().all()

    near = [
        (_haversine_m(lat, lng, r["lat"], r["lng"]), r)
        for r in rows
    ]
    near = [(d, r) for d, r in near if d <= ARRIVAL_RADIUS_M]
    if not near:
        return None

    _d, best = min(near, key=lambda x: x[0])  # 最近傍1件
    return {
        "going_id": best["going_id"],
        "store_id": best["store_id"],
        "store_name": best["store_name"],
    }
