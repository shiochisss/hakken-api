"""F8 着いたよ／着いたバナーの判定テスト（DB・ネットワーク不要）。

B-12/B-13 の実装 2026-07-26 で追加。距離判定と「バナーに出す1件」の選び方だけを
純関数レベルで固定する（DBアクセスを伴う所有チェック等は実DBでの手動確認に回す）。

実行: (.venv 有効化後)  python -m tests.test_arrival
pytest 未導入のため素の assert で書く（test_f9_stops.py と同じ流儀・新規依存なし）。
"""
from __future__ import annotations

from app.routers import arrival as a
from app.routers.search import _haversine_m

# 練馬駅あたりを基準にする
LAT, LNG = 35.738136, 139.653455


def _offset_north(lat: float, meters: float) -> float:
    """真北に meters だけずらした緯度（_haversine_m と同じ球で換算）。"""
    import math
    return lat + meters / (math.radians(1.0) * 6371000.0)


def test_judge_boundary():
    """境界（ちょうど150m）は verified 側に含める。"""
    assert a.judge(0) == "verified"
    assert a.judge(a.ARRIVAL_RADIUS_M - 1) == "verified"
    assert a.judge(a.ARRIVAL_RADIUS_M) == "verified"
    assert a.judge(a.ARRIVAL_RADIUS_M + 1) == "pending"
    assert a.judge(1000) == "pending"


def test_judge_with_real_distance():
    """実座標から距離を出して判定しても期待どおりになること。"""
    near = _offset_north(LAT, 100)     # 100m 北
    far = _offset_north(LAT, 400)      # 400m 北
    assert a.judge(_haversine_m(LAT, LNG, near, LNG)) == "verified"
    assert a.judge(_haversine_m(LAT, LNG, far, LNG)) == "pending"


def test_radius_is_provisional_value():
    """暫定値が変わったらテストの前提も見直す（変更検知のための固定）。"""
    assert a.ARRIVAL_RADIUS_M == 150
    assert a.BANNER_WINDOW_HOURS == 48


def _pick_banner(rows, lat, lng):
    """arrival_banner の「150m以内で最近傍1件」の選び方（実装と同じロジック）。"""
    near = [(_haversine_m(lat, lng, r["lat"], r["lng"]), r) for r in rows]
    near = [(d, r) for d, r in near if d <= a.ARRIVAL_RADIUS_M]
    if not near:
        return None
    return min(near, key=lambda x: x[0])[1]


def test_banner_picks_nearest_within_radius():
    """圏内の候補が複数あれば最近傍1件。圏外は候補にしない。"""
    rows = [
        {"going_id": 1, "store_id": 11, "store_name": "遠い店", "lat": _offset_north(LAT, 500), "lng": LNG},
        {"going_id": 2, "store_id": 12, "store_name": "近い店", "lat": _offset_north(LAT, 30), "lng": LNG},
        {"going_id": 3, "store_id": 13, "store_name": "中くらいの店", "lat": _offset_north(LAT, 120), "lng": LNG},
    ]
    got = _pick_banner(rows, LAT, LNG)
    assert got is not None and got["going_id"] == 2


def test_banner_none_when_all_far():
    """全部が圏外なら null（バナーを出さない）。"""
    rows = [
        {"going_id": 1, "store_id": 11, "store_name": "遠い店", "lat": _offset_north(LAT, 500), "lng": LNG},
        {"going_id": 2, "store_id": 12, "store_name": "もっと遠い店", "lat": _offset_north(LAT, 900), "lng": LNG},
    ]
    assert _pick_banner(rows, LAT, LNG) is None


def test_banner_empty_candidates():
    """候補が0件（未着の行が無い・48h超で除外済み）なら null。"""
    assert _pick_banner([], LAT, LNG) is None


def main():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok: {t.__name__}")
    print(f"全 {len(tests)} テスト成功")


if __name__ == "__main__":
    main()
