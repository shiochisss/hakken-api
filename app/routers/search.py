"""GET /api/search — 逆引き検索（F4・API設計書 B-6）。

現在地・楽条件から「楽に行ける店」を楽な順で返す。本アプリの心臓。

処理（B-6 の6・7）:
  1) 現在地から半径R（= walk_max 分に相当する直線距離 = walk_max×80÷1.3 m）内の
     stops を候補にし、各停の walk1（現在地→乗車停の徒歩分＝直線×1.3÷80）を算出。
     ※stops は **SQL 側で矩形（_search_bbox）に絞ってから読む**。以前は毎リクエストで
       全件（23区拡大後は 6,087 停）をロードしており、これが応答時間の主要因だった。
       矩形は円を包むだけの一次絞り込みで、円の判定は従来どおり haversine が行う
       ＝結果は全件ロード時と完全に一致する（tests/test_search_bbox.py で担保）。
  2) `reach WHERE boarding_stop_id IN (候補)` を1回引き、店×乗車停の到達行を取得。
  3) walk1+walk2≤walk_max・ride≤ride_max・walk1+ride+walk2≤total_max・transfer で
     フィルタ（待ち時間は捨象＝到達=walk1+ride+walk2）。
  4) 店ごとに最小 total の1行に畳む。stores 結合（status='営業中' かつ is_listed=true）。
  5) photo 解決（store_photos の approved/is_primary → none）。
     ※hotpepper_url は「店ページ」のURLで画像ではないため photo.ref には使わない
       （<img src> に入れると必ず読み込み失敗する）。ホットペッパーの画像URL取得は
       API連携が必要＝別件・未実装。
  6) ORDER BY total → walk1 → store_id。
  7) 起点（現在地）を住所ラベルに解決して `meta.origin` に載せ、`sessions` にも記録する
     （2026-07-27 追加。S2 の「〈住所〉から探しています」＝実機で「現在地がどこからなのか
     分からず信ぴょう性が薄い」と指摘されたため）。外部APIは呼ばない＝同梱した町丁目
     代表点の最寄り探索（`app/services/origin.py`）。**preview=1 のときは行わない**。
  preview=1 は items を省き件数のみ。0件時は relax_suggestions（walk_max+5分の件数）を返す。

TBD（DB設計書9章・確定した暫定値）:
  - 探索半径R / 第2ソートキー（#10）: R=walk_max×80÷1.3m、同total時 walk1昇順→store_id。
  - relax_suggestions 算出: 0件時に walk_max+5分での件数を1件返す（初期版）。
  - category（#12）: 2026-07-28 に実装（それまで受け取るだけで未使用＝どのチップを押しても
    全件が返っていた）。対応表は _CATEGORY_SQL の4キー。掲載146店では `bakery`・`sento` が
    該当0件だが、**キーは残して「押すと正しく0件」になるようにした**（DB設計書9章#12）。
  - SAS URL 発行（#14）: 未実装。store_photos は参照するが own/user の ref は当面 None。
"""
from __future__ import annotations

import math

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from app.db import get_engine
from app.deps import CurrentSession, get_current_session
# 球モデルは app/geo.py の1定義を共有する（起点ラベルの最寄り探索とも揃える）。
# 従来名 _haversine_m / EARTH_R_M で再公開しているので stores.py・arrival.py・テストは無変更。
from app.geo import EARTH_R_M, haversine_m as _haversine_m
from app.services import origin as origin_service
# 「本数少なめ」のしきい値は生成側（バッチ）に定義がある。二重定義を避けて import する。
from batch.route_segments import FEW_TRIPS_THRESHOLD

router = APIRouter()

WALK_DETOUR = 1.3           # 直線→道のり係数（store_stops と同一）
WALK_SPEED_M_PER_MIN = 80   # 徒歩速度 m/分
_TRANSFERS = {"none", "hub1"}
RELAX_WALK_DELTA = 5        # 0件時の緩和提案（walk_max +5分）

# カテゴリチップのキー → stores の分類条件（DB設計書9章#12・2026-07-28 確定）。
# 値は**固定の SQL 断片**で、キーはホワイトリスト検証を通ったものしか使わない（注入経路なし）。
#
# 2026-07-28 まで search は category を受け取るだけで WHERE に使っておらず、
# **どのチップを押しても全件が返っていた**（本番で発見）。
#
# 掲載146店の実測では `bakery`（category_s='パン'）と `sento`（category_l='銭湯'）は
# **該当0件**。それでも**キーは4つとも残す**（2026-07-28 判断）。押した結果が正しく0件に
# なるほうが、チップを消すより実態を伝えられるため（掲載が増えれば自動で出る／
# フロントとサーバの2箇所を同時に直す運用も要らない）。
#
# ※ food で coalesce しているのは、category_s が NULL の店（実測5店）を落とさないため。
#   設計書の SQL は `s.category_s <> 'パン'` だが、NULL <> 'パン' は NULL 判定になり
#   その店が「ごはん」から消える。
# ※ category_s は**自由記述74種類**（「イタリア料理」「コーヒースタンド」等・多くが1店）。
#   bakery を実際に機能させるには category_l に分類を新設し、キュレーション側の語彙を
#   正規化する必要がある（DB設計書9章#12の新TBD）。
_CATEGORY_SQL = {
    "cafe": "s.category_l = 'カフェ'",
    "food": "s.category_l = '飲食' AND coalesce(s.category_s, '') <> 'パン'",
    "bakery": "s.category_s = 'パン'",
    "sento": "s.category_l = '銭湯'",
}

# 現在地の妥当域（日本全体をカバー）。範囲外は 400（B-6 は 400 を規定＝Query(ge/le)の422でなく手動400に統一）。
LAT_MIN, LAT_MAX = 20.0, 46.0
LNG_MIN, LNG_MAX = 122.0, 154.0


def _walk_min(distance_m: float) -> int:
    return int(round(distance_m * WALK_DETOUR / WALK_SPEED_M_PER_MIN))


def _search_bbox(lat: float, lng: float, walk_max: int) -> dict[str, float]:
    """walk1 ≤ walk_max 分と判定されうる停を**必ず含む**緯度経度の矩形を返す。

    stops を SQL 側で一次絞り込みするために使う（23区拡大で 1,491→6,087 停になり、
    毎リクエストの全件ロードが検索応答の主要因になっていた・DB設計書9章）。
    絞り込みはあくまで一次で、正確な円の判定は従来どおり _nearby_walk1 が haversine で行う。
    したがって**矩形が円を包んでさえいれば結果は全件ロードと完全に一致する**。

    包む条件は2つ:
      1) 半径は (walk_max + 0.5) 分ぶんを使う。_walk_min は四捨五入なので、
         「walk_max 分」と判定される最大距離は walk_max 分ちょうどより 0.5 分ぶん遠い。
      2) 度への換算は _haversine_m と同じ球（EARTH_R_M）で行う。別の地球モデルを使うと
         円と矩形が微妙にズレて、境界の停を取りこぼしうる。
    """
    r_m = (walk_max + 0.5) * WALK_SPEED_M_PER_MIN / WALK_DETOUR
    m_per_deg = math.radians(1.0) * EARTH_R_M       # 緯度1度あたりのメートル
    dlat = r_m / m_per_deg
    # 経度1度の長さは cos(緯度) に比例して縮む＝高緯度ほど dlng は大きくなる。矩形の南北端の
    # うち cos が小さい方（＝dlng が大きい方）を採らないと、その端で矩形が足りなくなる。
    cos_min = min(abs(math.cos(math.radians(lat + dlat))), abs(math.cos(math.radians(lat - dlat))))
    dlng = 180.0 if cos_min < 1e-9 else min(180.0, r_m / (m_per_deg * cos_min))  # 極付近の0除算よけ
    return {"lat_min": lat - dlat, "lat_max": lat + dlat,
            "lng_min": lng - dlng, "lng_max": lng + dlng}


def _nearby_walk1(stops: list, lat: float, lng: float, walk_max: int) -> dict[int, int]:
    """現在地から walk1 ≤ walk_max 分の停 → {stop_id: walk1_min}。

    stops の要素は (id, lat, lng) 以降を無視する＝4要素目に name が付いていてもよい
    （起点ラベルのフォールバック用に name も引いているため）。
    """
    out: dict[int, int] = {}
    for st in stops:
        sid, slat, slng = st[0], st[1], st[2]
        w1 = _walk_min(_haversine_m(lat, lng, slat, slng))
        if w1 <= walk_max:
            out[sid] = w1
    return out


def _nearest_stop_name(stops: list, lat: float, lng: float) -> str | None:
    """現在地に最も近い停の名前。起点の住所が出せなかったときの代替表示に使う。"""
    best: tuple[float, str] | None = None
    for st in stops:
        if len(st) < 4 or not st[3]:
            continue
        d = _haversine_m(lat, lng, st[1], st[2])
        if best is None or d < best[0]:
            best = (d, st[3])
    return best[1] if best else None


# ラベル整形など純粋な組み立てはインラインで足りるため関数化しない（既存router同様の素直さ）


def _best_by_store(reach_rows, nearby: dict[int, int], walk_max: int, ride_max: int,
                   total_max: int, transfer: str) -> dict[int, dict]:
    """到達行を条件でフィルタし、店ごとに最小 total（同点は walk1 昇順）の1行に畳む。"""
    best: dict[int, dict] = {}
    for r in reach_rows:
        # transfer: none 指定なら直行のみ。hub1 指定なら直行＋乗換1回の両方可（B-6 SQL準拠）。
        # ※値名は hub1 のままだが意味は「乗換1回」。乗換停は任意の停（2026-07-26・batch/reach.py 参照）
        if transfer == "none" and r["transfer"] != "none":
            continue
        w1 = nearby.get(r["boarding_stop_id"])
        if w1 is None:
            continue
        if w1 + r["walk2_min"] > walk_max:
            continue
        if r["ride_min"] > ride_max:
            continue
        total = w1 + r["ride_min"] + r["walk2_min"]
        if total > total_max:
            continue
        cur = best.get(r["store_id"])
        cand = {"row": r, "walk1": w1, "total": total}
        if cur is None or (total, w1) < (cur["total"], cur["walk1"]):
            best[r["store_id"]] = cand
    return best


@router.get("/api/search")
def search(
    lat: float = Query(...),
    lng: float = Query(...),
    walk_max: int = Query(...),
    ride_max: int = Query(...),
    total_max: int = Query(...),
    transfer: str = Query("none"),
    category: str | None = Query(None),   # チップのキー（_CATEGORY_SQL のみ許可）。null=すべて
    preview: str | None = Query(None),
    session: CurrentSession = Depends(get_current_session),
):
    # バリデーション（違反は 400）。上限は設けない（大きな値でもクエリは破綻しない）。
    if transfer not in _TRANSFERS:
        raise HTTPException(status_code=400, detail="invalid transfer")
    # category はホワイトリスト（B-6）。未知のキーを黙って「すべて」に落とすと、
    # 絞れていないのに絞れたように見えて気付けないため 400 にする。
    if category is not None and category not in _CATEGORY_SQL:
        raise HTTPException(status_code=400, detail="invalid category")
    for _name, _v in (("walk_max", walk_max), ("ride_max", ride_max), ("total_max", total_max)):
        if _v < 1:
            raise HTTPException(status_code=400, detail=f"{_name} must be >= 1")
    if not (LAT_MIN <= lat <= LAT_MAX):
        raise HTTPException(status_code=400, detail="lat out of range")
    if not (LNG_MIN <= lng <= LNG_MAX):
        raise HTTPException(status_code=400, detail="lng out of range")

    with get_engine().begin() as conn:
        # 起点の住所が出せなかったときの代替表示に使う最寄停名（最初の run で1回だけ拾う）
        nearest_stop: str | None = None

        def run(w_max: int) -> dict[int, dict]:
            nonlocal nearest_stop
            # stops は矩形で一次絞り込みしてから読む（全件ロードしない・_search_bbox 参照）。
            # 円の外周ぶんは余分に返るが、直後の _nearby_walk1 が haversine で正確に落とす。
            stops = [
                (row["id"], row["lat"], row["lng"], row["name"])
                for row in conn.execute(
                    text(
                        """
                        SELECT id, lat, lng, name FROM stops
                        WHERE lat BETWEEN :lat_min AND :lat_max
                          AND lng BETWEEN :lng_min AND :lng_max
                        """
                    ),
                    _search_bbox(lat, lng, w_max),
                ).mappings()
            ]
            if nearest_stop is None:
                nearest_stop = _nearest_stop_name(stops, lat, lng)
            nearby = _nearby_walk1(stops, lat, lng, w_max)
            if not nearby:
                return {}
            # カテゴリ条件は固定の SQL 断片（_CATEGORY_SQL・キーは検証済み）。
            # WHERE に入れることで preview の件数・0件時の緩和提案にも同じ絞りが効く。
            cat_sql = f"AND ({_CATEGORY_SQL[category]})" if category else ""
            rows = conn.execute(
                text(
                    f"""
                    SELECT r.store_id, r.boarding_stop_id, r.ride_min, r.walk2_min,
                           r.transfer, r.via_hub_id, r.alight_stop_id, r.route_label,
                           r.min_trip_count,
                           s.name AS store_name, s.category_l, s.category_s, s.status,
                           s.address, s.area_label, s.lat AS store_lat, s.lng AS store_lng,
                           s.gmaps_url,
                           bs.name AS boarding_name, als.name AS alight_name, hub.name AS hub_name
                    FROM reach r
                    JOIN stores s ON s.id = r.store_id
                    JOIN stops bs ON bs.id = r.boarding_stop_id
                    JOIN stops als ON als.id = r.alight_stop_id
                    LEFT JOIN stops hub ON hub.id = r.via_hub_id
                    WHERE r.boarding_stop_id = ANY(:ids)
                      AND s.status = '営業中' AND s.is_listed = true
                      {cat_sql}
                    """
                ),
                {"ids": list(nearby.keys())},
            ).mappings().all()
            return _best_by_store(rows, nearby, w_max, ride_max, total_max, transfer)

        best = run(walk_max)
        count = len(best)

        # preview=1（S2-b のライブプレビュー）は件数のみ。連打されるので起点の解決・記録も
        # しない（表示に使わないため不要）。
        if preview == "1":
            return {"items": [], "meta": {"count": count, "relax_suggestions": []}}

        # 起点（現在地）の住所ラベル。S2 の「〈住所〉から探しています」に使い、同時に
        # セッションにも記録する（「その提案はどこ起点だったか」を後から辿るため）。
        # 外部APIは呼ばず同梱データの最寄り探索で解決する（app/services/origin.py）。
        origin = origin_service.resolve_origin(lat, lng, nearest_stop)
        origin_service.save_session_origin(conn, session.sid, lat, lng, origin["label"])

        # 0件時のみ relax 提案（walk_max +5分での件数）。
        # **緩めても0件なら提案しない**（2026-07-28）。カテゴリで絞った結果の0件は歩きを
        # 緩めても増えないため、「歩きを+5分ゆるめる（0件）」という押しても何も起きない
        # ボタンが出てしまう。0件を行き止まりにしない趣旨は「次の一手がある時に出す」で足りる。
        relax: list[dict] = []
        if count == 0:
            relaxed = run(walk_max + RELAX_WALK_DELTA)
            if relaxed:
                relax = [{"param": "walk_max", "delta": RELAX_WALK_DELTA, "count": len(relaxed)}]

        # photo 解決（対象店の approved/is_primary を1回引く）
        photo_by_store: dict[int, dict] = {}
        if best:
            for row in conn.execute(
                text(
                    """
                    SELECT DISTINCT ON (store_id) store_id, source
                    FROM store_photos
                    WHERE status = 'approved' AND is_primary = true AND store_id = ANY(:sids)
                    ORDER BY store_id, sort_order
                    """
                ),
                {"sids": list(best.keys())},
            ).mappings():
                photo_by_store[row["store_id"]] = {"source": row["source"], "ref": None}  # SAS未発行=#14

    # items 組み立て（total → walk1 → store_id 昇順）
    items = []
    for store_id, b in sorted(best.items(), key=lambda kv: (kv[1]["total"], kv[1]["walk1"], kv[0])):
        r = b["row"]
        # own/user（ref は SAS 未実装のため None）→ 無ければ none。
        # hotpepper_url は画像URLではないので使わない（上の docstring 5 参照）。
        photo = photo_by_store.get(store_id, {"source": "none", "ref": None})
        items.append({
            "store_id": store_id,
            "name": r["store_name"],
            "category_l": r["category_l"],
            "category_s": r["category_s"],
            "status": r["status"],
            "photo": photo,
            "raku": {
                "walk1": b["walk1"], "ride": r["ride_min"], "walk2": r["walk2_min"],
                "total": b["total"], "transfer": r["transfer"],
                "via_hub": r["hub_name"] if r["transfer"] == "hub1" else None,
            },
            # 「本数少なめ」＝土日昼の便数がしきい値未満。除外はせずバッジで開示する（引き継ぎ資料4章）。
            # min_trip_count が NULL（バッチ未実行）のときは false＝バッジを出さない側に倒す。
            "few_trips": r["min_trip_count"] is not None and r["min_trip_count"] < FEW_TRIPS_THRESHOLD,
            "boarding_stop": r["boarding_name"],
            "alight_stop": r["alight_name"],
            "route_label": r["route_label"],
            "address": r["address"],
            "area_label": r["area_label"],
            "lat": r["store_lat"],
            "lng": r["store_lng"],
            "gmaps_url": r["gmaps_url"],
        })

    return {
        "items": items,
        "meta": {"count": len(items), "relax_suggestions": relax, "origin": origin},
    }
