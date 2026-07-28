"""GET /api/stores/{id} — 店詳細（F4・API設計書 B-7）。

単一店の詳細（現在地からの楽さ内訳込み）を返す。S3直リンク時に使用。
レスポンスは B-6 の items[] 要素と同形（StoreItem）。

設計書 B-7 の記載どおり:
  - パス: id（int）／クエリ: lat・lng（必須）。
  - レスポンス: StoreItem（B-6 items と同形。raku・boarding/alight・route_label・photo 含む）。
  - エラー: 401（未認証）／404（存在しない・非配信＝status≠営業中 or is_listed=false）。
  - 処理: 現在地からの乗車停→当該店の**最小 total 1行**を取得（reach 由来）。到達=walk1+ride+walk2（待ち時間捨象）。
  - 副作用: **`sessions` の起点（origin_*）を更新する**（2026-07-27 追加。設計書では「副作用なし」
    だったが、直後の「ここ行く」が宣言時点の起点を記録できるようにするため。GET が書くのは
    A-8「GET は冪等」の例外だが、同じ依存が既に last_seen_at を更新している前例に沿う）。

※「半径R」について: B-7 はクエリで条件を取らないため設計書に R の定義が無い（記載なし）。
  当該店の reach 行は少数のため、**候補を半径で絞らず全 boarding_stop から選ぶ**
  （R は B-6 での候補絞り最適化であり、最小 total の正しさには不要）。
※【2026-07-28 修正】ただし**楽条件は適用する**。それまで walk_max/ride_max/total_max/transfer を
  一切見ずに最小 total を選んでいたため、**S2 と S3 で所要時間が食い違っていた**
  （本番実測: 同じ店が S2 で「歩10＋バス15＝29分（直行）」、S3 で「歩0＋バス14＝18分（乗換1回）」。
  18分の経路は balance の transfer=none が除外していたものを S3 だけが拾っていた）。
  条件はクエリではなく `user_conditions` から読む（API契約・フロントを変えずに済むため）。
  条件を満たす経路が1件も無いときは条件なしで最良を返し `out_of_conditions` を立てる
  （A-2 方式。マイリスト/お気に入りから開いた店の情報を失わないため）。
※lat/lng の範囲チェック(400)は B-7 の error 節に明記が無いが、B-6 と整合させて実装（下記コメント）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from app.db import get_engine
from app.deps import CurrentSession, get_current_session
from app.services import origin as origin_service
# 徒歩式・座標妥当域は B-6 と同一のものを再利用（walk1 計算の一貫性を担保）
from app.routers.search import (
    FEW_TRIPS_THRESHOLD, LAT_MAX, LAT_MIN, LNG_MAX, LNG_MIN, _haversine_m, _walk_min,
    walk_only_info,
)

router = APIRouter()

# walk_only の歩き上限のフォールバック。通常は user_conditions.walk_max を使うので、
# これが効くのは**楽条件が未設定のユーザー**だけ（初回は /setup へ飛ぶので実質起きない）。
# 値はプリセット最大の 20分（far_ok の walk_max）。
WALK_ONLY_MAX_MIN = 20


@router.get("/api/stores/{store_id}")
def get_store(
    store_id: int,
    lat: float = Query(...),
    lng: float = Query(...),
    session: CurrentSession = Depends(get_current_session),
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
                       r.alight_stop_id, r.route_label, r.min_trip_count,
                       s.name AS store_name, s.category_l, s.category_s, s.status,
                       s.address, s.area_label, s.lat AS store_lat, s.lng AS store_lng,
                       s.gmaps_url,
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

        # ユーザーの楽条件を読む（2026-07-28 追加）。B-6 と同じ条件で選ばないと
        # **S2 と S3 で所要時間が食い違う**（本番で発覚：同じ店が S2 29分／S3 18分。
        # 18分の経路は乗換1回で、balance の transfer=none では S2 が除外していた）。
        cond = conn.execute(
            text(
                """
                SELECT walk_max, ride_max, total_max, transfer
                FROM user_conditions WHERE user_id = :uid
                """
            ),
            {"uid": session.uid},
        ).mappings().first()

        # 現在地からの最小 total（同点は walk1 昇順）を選ぶ。
        # **まず条件を満たす経路から選び**、1つも無ければ条件なしで選び直して
        # out_of_conditions を立てる（A-2 方式・2026-07-28 ibes 判断）。
        # 条件外でも返すのは、マイリスト/お気に入りから開いた店の情報を失わないため
        # （マイリストは掲載フィルタをかけない＝B-11 v1.3確定 と同じ思想）。
        def pick(rs):
            b = None
            for r in rs:
                w1 = _walk_min(_haversine_m(lat, lng, r["b_lat"], r["b_lng"]))
                total = w1 + r["ride_min"] + r["walk2_min"]
                if b is None or (total, w1) < (b["total"], b["walk1"]):
                    b = {"row": r, "walk1": w1, "total": total}
            return b

        def violations(r, w1, total):
            """この経路が破っている条件。条件未設定なら空（＝判定しない）。"""
            if not cond:
                return {}
            return {
                "transfer": cond["transfer"] == "none" and r["transfer"] != "none",
                "walk": w1 + r["walk2_min"] > cond["walk_max"],
                "ride": r["ride_min"] > cond["ride_max"],
                "total": total > cond["total_max"],
            }

        ok = []
        for r in rows:
            w1 = _walk_min(_haversine_m(lat, lng, r["b_lat"], r["b_lng"]))
            if not any(violations(r, w1, w1 + r["ride_min"] + r["walk2_min"]).values()):
                ok.append(r)

        best = pick(ok) if ok else pick(rows)
        out_of_conditions = None
        if not ok:
            v = violations(best["row"], best["walk1"], best["total"])
            if any(v.values()):
                out_of_conditions = v

        # photo: store_photos(approved/is_primary) → none（SAS発行は#14で後日・ref=None）
        # ※hotpepper_url は「店ページ」のURLで画像ではないため photo.ref には使わない
        #   （<img src> に入れると必ず読み込み失敗する）。画像URL取得はAPI連携＝別件・未実装。
        row = best["row"]
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

        # 起点をセッションに記録する（2026-07-27）。S3 はマウント時に必ずここを呼ぶので、
        # 直後の「ここ行く」（B-10）が going_list へコピーする起点が最新になる。
        # レスポンスには載せない（起点の住所を出すのは S2 だけ＝今回のスコープ）。
        # フォールバック停名は「採用した経路の乗車停」＝厳密な最寄停ではないが、
        # 住所が解決できたときは使われないため実用上問題にならない。
        origin_service.save_session_origin(
            conn, session.sid, lat, lng,
            origin_service.resolve_origin(lat, lng, row["boarding_name"])["label"],
        )

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
        # B-6 と同じ判定（few_trips の意味は search.py のコメント参照）
        "few_trips": row["min_trip_count"] is not None and row["min_trip_count"] < FEW_TRIPS_THRESHOLD,
        # 徒歩の方が速いとき {minutes, distance_m}（B-6 と同じ walk_only_info）。
        # 歩き上限は**ユーザーの walk_max**を使う（未設定時のみ WALK_ONLY_MAX_MIN）。
        "walk_only": walk_only_info(
            _haversine_m(lat, lng, row["store_lat"], row["store_lng"]),
            best["total"], cond["walk_max"] if cond else WALK_ONLY_MAX_MIN,
        ),
        # いまの楽条件を満たさない経路を返しているとき、破っている条件を立てる。
        # 満たしているとき（＝S2と一致するとき）は null。
        "out_of_conditions": out_of_conditions,
        "boarding_stop": row["boarding_name"],
        "alight_stop": row["alight_name"],
        "route_label": row["route_label"],
        "address": row["address"],
        "area_label": row["area_label"],
        "lat": row["store_lat"],
        "lng": row["store_lng"],
        "gmaps_url": row["gmaps_url"],
    }
