"""起点ラベル（住所）の解決と丸めのテスト（DB・ネットワーク不要）。

守りたいこと:
  1. **位置の生値がDBに渡らない**（丸めが常に効く）＝プライバシー方針の担保（DB設計書1章-5）
  2. 実在の座標で妥当な住所が出る（同梱データと格子索引が壊れていない）
  3. データが無い場所・データ自体が無い環境でも**例外を出さず**フォールバックする
     （起点ラベルは付帯機能。ここで落ちて検索APIが500になるのが最悪のケース）

実行: (.venv 有効化後)  python -m tests.test_origin
pytest 未導入のため素の assert で書く（test_search_bbox.py と同じ流儀・新規依存なし）。
"""
from __future__ import annotations

from app.services import origin as o

# 検証エリアの実在地点（design文書・README で使っている基準点）
NERIMA = (35.7357, 139.6518)      # 練馬駅付近
EKODA = (35.7376, 139.6720)       # 江古田駅付近
SHINJUKU = (35.6896, 139.6917)    # 新宿駅付近（GSI API の例と同じ点）


def test_round_origin_truncates_to_3_decimals():
    """丸めが常に3桁になること。DBの NUMERIC(6,3) と粒度を揃える前提。"""
    lat, lng = o.round_origin(35.7357123456, 139.6518987654)
    assert lat == 35.736 and lng == 139.652
    # 丸めた値をもう一度丸めても変わらない（冪等）
    assert o.round_origin(lat, lng) == (lat, lng)
    # 桁数が3を超えないこと（文字列表現で確認＝生値が紛れ込んでいないか）
    for v in (lat, lng):
        frac = str(v).split(".")[1]
        assert len(frac) <= o.ORIGIN_PRECISION, f"{v} の小数桁が {len(frac)} 桁ある"


def test_round_origin_grid_is_about_110m():
    """3桁の格子が約110m であること（プライバシー説明の根拠になる数値）。"""
    from app.geo import haversine_m

    d = haversine_m(35.700, 139.650, 35.701, 139.650)
    assert 100 < d < 120, f"緯度0.001度が {d:.0f}m（想定は約110m）"


def test_nearest_label_returns_plausible_address():
    """検証エリアの各地点で、期待する区名を含む住所が返ること。"""
    cases = [(NERIMA, "練馬区"), (EKODA, ""), (SHINJUKU, "新宿区")]
    for (lat, lng), expect in cases:
        label = o.nearest_oaza_label(lat, lng)
        assert label, f"({lat},{lng}) で住所が解決できない（同梱データを確認）"
        assert label.startswith("東京都"), f"{label} が東京都で始まらない"
        if expect:
            assert expect in label, f"{label} に {expect} が含まれない"
    # 江古田は区境（練馬区／中野区）なのでどちらでも可＝区名は問わない
    assert "東京都" in (o.nearest_oaza_label(*EKODA) or "")


def test_nearby_points_give_the_same_label():
    """同じ110m格子の中の座標なら同じ住所になること（表示がちらつかない）。"""
    lat, lng = NERIMA
    base = o.nearest_oaza_label(lat, lng)
    for dlat, dlng in ((0.0002, 0.0), (0.0, 0.0002), (-0.0002, -0.0002)):
        assert o.nearest_oaza_label(lat + dlat, lng + dlng) == base


def test_resolve_origin_sources():
    """source の3分岐（フロントの表示分岐の契約）。"""
    r = o.resolve_origin(*NERIMA, fallback_stop="練馬駅北口")
    assert r["source"] == "oaza" and r["label"] and r["nearest_stop"] == "練馬駅北口"

    # 太平洋上＝データが1点も無い → 停名にフォールバック
    r = o.resolve_origin(30.0, 145.0, fallback_stop="どこかの停")
    assert r == {"label": None, "nearest_stop": "どこかの停", "source": "stop"}

    # 停名も無ければ none（フロントは従来文言「現在地から」に戻す）
    r = o.resolve_origin(30.0, 145.0, fallback_stop=None)
    assert r == {"label": None, "nearest_stop": None, "source": "none"}


def test_missing_data_file_does_not_raise():
    """同梱データが無い環境でも例外を出さず none になること（検索APIを落とさない）。"""
    saved_grid, saved_path = o._grid, o.CONFIG_PATH
    try:
        o._grid = None
        o.CONFIG_PATH = saved_path.parent / "does_not_exist.json"
        assert o.nearest_oaza_label(*NERIMA) is None
        assert o.resolve_origin(*NERIMA, fallback_stop=None)["source"] == "none"
    finally:
        o._grid, o.CONFIG_PATH = saved_grid, saved_path
    # 後片付けが効いていること（他のテストに影響しない）
    o._grid = None
    assert o.nearest_oaza_label(*NERIMA)


def test_label_fits_db_column():
    """全点のラベルが VARCHAR(120) に収まること（本番で 22001 を出さない）。"""
    over = [p for cell in o._load_grid().values() for p in cell if len(p[2]) > o.MAX_LABEL_LEN]
    assert not over, f"120文字を超えるラベルが {len(over)} 件ある: {over[:3]}"


def test_grid_index_covers_all_points():
    """格子索引に全点が入っていること（生成データと索引の件数が一致）。"""
    import json

    with open(o.CONFIG_PATH, encoding="utf-8") as f:
        n_file = len(json.load(f)["points"])
    n_grid = sum(len(v) for v in o._load_grid().values())
    assert n_file == n_grid, f"データ {n_file} 点に対し索引 {n_grid} 点"
    assert n_grid > 1000, f"点が少なすぎる（{n_grid}）＝生成が失敗している疑い"


def main():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok: {t.__name__}")
    print(f"全 {len(tests)} テスト成功")


if __name__ == "__main__":
    main()
