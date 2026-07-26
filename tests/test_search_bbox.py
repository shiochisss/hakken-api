"""検索の stops 一次絞り込み（_search_bbox）のテスト（DB・ネットワーク不要）。

矩形は「全件ロードと同じ結果になる」ことが唯一の要件。狭いと本来ヒットする停を
取りこぼし、検索結果から店が消える（気づきにくい壊れ方をする）ため、
**矩形が円を包んでいること**を網羅的に確認する。

実行: (.venv 有効化後)  python -m tests.test_search_bbox
pytest 未導入のため素の assert で書く（test_f9_stops.py と同じ流儀・新規依存なし）。
"""
from __future__ import annotations

import math
import random

from app.routers import search as s


def _in_bbox(bb: dict, lat: float, lng: float) -> bool:
    return bb["lat_min"] <= lat <= bb["lat_max"] and bb["lng_min"] <= lng <= bb["lng_max"]


def test_bbox_contains_every_stop_the_circle_keeps():
    """乱数で撒いた停について「円に入る停は必ず矩形にも入る」ことを確認する。

    逆（矩形に入るが円に入らない）は許される＝一次絞り込みなので余分に返ってよい。
    """
    rnd = random.Random(20260726)  # 再現性のため固定シード
    centers = [(35.738136, 139.653455), (35.68, 139.76), (43.06, 141.35), (26.21, 127.68)]
    misses = 0
    for lat0, lng0 in centers:
        for walk_max in (1, 5, 10, 15, 20, 30, 60):
            bb = s._search_bbox(lat0, lng0, walk_max)
            for _ in range(400):
                # 矩形より一回り広い範囲に撒く（境界の外側も試す）
                lat = lat0 + rnd.uniform(-1.5, 1.5) * (bb["lat_max"] - lat0)
                lng = lng0 + rnd.uniform(-1.5, 1.5) * (bb["lng_max"] - lng0)
                w1 = s._walk_min(s._haversine_m(lat0, lng0, lat, lng))
                if w1 <= walk_max and not _in_bbox(bb, lat, lng):
                    misses += 1
    assert misses == 0, f"円に入るのに矩形から漏れた停が {misses} 件ある（結果が欠ける）"


def test_bbox_covers_rounding_boundary():
    """_walk_min は四捨五入なので、walk_max 分と判定される最遠の停まで矩形が届くこと。

    半径に +0.5 分を足し忘れると、ちょうど境界にある停を落とす。
    """
    lat0, lng0 = 35.738136, 139.653455
    for walk_max in (5, 10, 15, 20):
        # 「walk_max 分」と丸められる最大距離（ぎりぎり内側）
        r_m = (walk_max + 0.4999) * s.WALK_SPEED_M_PER_MIN / s.WALK_DETOUR
        bb = s._search_bbox(lat0, lng0, walk_max)
        dlat = r_m / (math.radians(1.0) * s.EARTH_R_M)
        for lat, lng in ((lat0 + dlat, lng0), (lat0 - dlat, lng0)):
            assert s._walk_min(s._haversine_m(lat0, lng0, lat, lng)) == walk_max
            assert _in_bbox(bb, lat, lng), f"境界の停が矩形外（walk_max={walk_max}）"


def test_nearby_walk1_same_with_and_without_bbox():
    """全件を渡した場合と、矩形で絞ってから渡した場合で _nearby_walk1 の結果が一致すること。"""
    rnd = random.Random(1)
    lat0, lng0 = 35.738136, 139.653455
    # 23区くらいの広がりに 3,000 停を撒く
    stops = [(i, lat0 + rnd.uniform(-0.2, 0.2), lng0 + rnd.uniform(-0.25, 0.25))
             for i in range(3000)]
    for walk_max in (5, 15, 20):
        bb = s._search_bbox(lat0, lng0, walk_max)
        filtered = [st for st in stops if _in_bbox(bb, st[1], st[2])]
        assert s._nearby_walk1(stops, lat0, lng0, walk_max) == \
               s._nearby_walk1(filtered, lat0, lng0, walk_max)
        assert len(filtered) < len(stops)  # 絞り込みが効いていること


def test_bbox_shrinks_with_walk_max():
    """walk_max が小さいほど矩形も小さいこと（絞り込みの効きが条件に比例する）。"""
    lat0, lng0 = 35.738136, 139.653455
    prev = None
    for walk_max in (5, 10, 20, 40):
        bb = s._search_bbox(lat0, lng0, walk_max)
        size = (bb["lat_max"] - bb["lat_min"]) * (bb["lng_max"] - bb["lng_min"])
        if prev is not None:
            assert size > prev
        prev = size


def main():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok: {t.__name__}")
    print(f"全 {len(tests)} テスト成功")


if __name__ == "__main__":
    main()
