"""route_segments / reach の純関数テスト（DB・ネットワーク不要）。

trip_count（本数少なめ）対応 2026-07-26 で追加。
  - build_segments が (ride_min, trip_count) を返すこと
  - trip_count の母数が「土日 かつ 昼の時間帯に乗車停を出発する便」であること
  - build_reach が min_trip_count を持ち回すこと（hub経由は2区間の最小値）
  - 同点時の優先順（最短 → 直行 → 便数が多い）

実行: (.venv 有効化後)  python -m tests.test_route_segments
pytest 未導入のため素の assert で書く（test_f9_stops.py と同じ流儀・新規依存なし）。
"""
from __future__ import annotations

from batch import reach as rc, route_segments as rs

# 便を1本ぶん組み立てる。stop_times.txt の行の並び（trip_id・stop_sequence・時刻・stop_id）。
def _trip(trip_id: str, stops: list[tuple[str, str]]) -> list[dict]:
    """stops = [(stop_id, "HH:MM:SS"), ...]。到着=出発として扱う（テストでは停車時間を持たせない）。"""
    return [
        {"trip_id": trip_id, "stop_sequence": str(i), "stop_id": sid,
         "arrival_time": t, "departure_time": t}
        for i, (sid, t) in enumerate(stops, start=1)
    ]


def _tally() -> dict:
    return {k: 0 for k in rs._TALLY_KEYS}


# 全便を土日運行の同一路線に載せる共通設定
TRIPS = {f"t{i}": ("R1", "svc_weekend") for i in range(1, 10)}
WEEKEND = {"svc_weekend"}
SCOPE = {"seibu:A", "seibu:B", "seibu:C"}


def test_returns_ride_and_trip_count():
    """戻り値が (ride_min, trip_count) のタプルになっていること。"""
    rows = _trip("t1", [("A", "12:00:00"), ("B", "12:10:00")])
    segs = rs.build_segments("seibu", rows, TRIPS, WEEKEND, _tally(), SCOPE)
    assert segs[("seibu:R1", "seibu:A", "seibu:B")] == (10, 1)


def test_trip_count_counts_all_trips():
    """同一 (route, 乗車, 降車) に3便あれば trip_count == 3。ride_min はその中央値。"""
    rows = (
        _trip("t1", [("A", "10:00:00"), ("B", "10:08:00")])   # 8分
        + _trip("t2", [("A", "12:00:00"), ("B", "12:10:00")])  # 10分
        + _trip("t3", [("A", "14:00:00"), ("B", "14:12:00")])  # 12分
    )
    segs = rs.build_segments("seibu", rows, TRIPS, WEEKEND, _tally(), SCOPE)
    ride_min, trip_count = segs[("seibu:R1", "seibu:A", "seibu:B")]
    assert trip_count == 3
    assert ride_min == 10  # 8/10/12 の中央値


def test_trip_count_excludes_out_of_lunch():
    """昼の時間帯（LUNCH_*）の外に乗車停を出発する便は母数に入らない。"""
    rows = (
        _trip("t1", [("A", "12:00:00"), ("B", "12:10:00")])   # 昼＝採用
        + _trip("t2", [("A", "07:00:00"), ("B", "07:10:00")])  # 早朝＝除外
        + _trip("t3", [("A", "20:00:00"), ("B", "20:10:00")])  # 夜＝除外
    )
    segs = rs.build_segments("seibu", rows, TRIPS, WEEKEND, _tally(), SCOPE)
    assert segs[("seibu:R1", "seibu:A", "seibu:B")] == (10, 1)


def test_trip_count_excludes_weekday():
    """土日運行でない service_id の便は母数に入らない（区間ごと生成されない）。"""
    trips = {"t1": ("R1", "svc_weekend"), "t2": ("R1", "svc_weekday")}
    rows = (
        _trip("t1", [("A", "12:00:00"), ("B", "12:10:00")])
        + _trip("t2", [("A", "13:00:00"), ("B", "13:10:00")])
    )
    segs = rs.build_segments("seibu", rows, trips, WEEKEND, _tally(), SCOPE)
    assert segs[("seibu:R1", "seibu:A", "seibu:B")] == (10, 1)


def test_downstream_pairs_have_independent_counts():
    """下流全停ペア化の要点：通しで走る便しか長い区間には該当しない。

    A→B は2便あるが、C まで行くのは1便だけ＝A→C は trip_count が 1 になる。
    これが「本数少なめ」が長い区間に偏る理由（引き継ぎ資料4章）。
    """
    rows = (
        _trip("t1", [("A", "12:00:00"), ("B", "12:10:00"), ("C", "12:20:00")])
        + _trip("t2", [("A", "13:00:00"), ("B", "13:10:00")])  # B止まり
    )
    segs = rs.build_segments("seibu", rows, TRIPS, WEEKEND, _tally(), SCOPE)
    assert segs[("seibu:R1", "seibu:A", "seibu:B")][1] == 2
    assert segs[("seibu:R1", "seibu:A", "seibu:C")][1] == 1
    assert segs[("seibu:R1", "seibu:B", "seibu:C")][1] == 1


# ============================================================
# reach 側（build_reach）
# ============================================================

def test_reach_direct_carries_trip_count():
    """直行では区間の trip_count がそのまま min_trip_count になる。"""
    rows, _t = rc.build_reach(
        store_stops={100: [(1, 5)]},                       # 店100 の降車停=1・徒歩5分
        seg_by_alight={1: [(2, 10, 900, 7)]},              # 停2 →10分→ 停1（路線900・7便）
        route_label={900: "テスト行き"},
    )
    assert len(rows) == 1
    assert rows[0]["min_trip_count"] == 7
    assert rows[0]["transfer"] == "none" and rows[0]["via_hub_id"] is None


def test_reach_transfer_at_any_stop():
    """乗換停は任意の停でよい（2026-07-26・9章#16）。以前は is_hub の停に限られていた。"""
    rows, _t = rc.build_reach(
        store_stops={100: [(1, 5)]},
        seg_by_alight={
            1: [(9, 10, 900, 8)],    # 停9 →10分→ 停1（8便）※hub ではない普通の停
            9: [(2, 6, 901, 3)],     # 停2 →6分→ 停9（3便）
        },
        route_label={900: "2本目", 901: "1本目"},
    )
    tr = [r for r in rows if r["transfer"] == "hub1"]
    assert len(tr) == 1
    assert tr[0]["boarding_stop_id"] == 2 and tr[0]["via_hub_id"] == 9
    assert tr[0]["ride_min"] == 16                 # 6+10。ペナルティは ride に含めない
    assert tr[0]["min_trip_count"] == 3            # min(3, 8)＝ボトルネック
    assert tr[0]["route_label"] == "1本目"          # 表示は乗車する1本目


def test_reach_does_not_search_two_transfers():
    """乗換2回（3区間）は探索しない＝MAX_SEGMENTS=2。"""
    rows, _t = rc.build_reach(
        store_stops={100: [(1, 5)]},
        seg_by_alight={
            1: [(9, 5, 900, 5)],     # 停9 → 停1
            9: [(8, 5, 901, 5)],     # 停8 → 停9   （ここまでは乗換1回で到達可）
            8: [(7, 5, 902, 5)],     # 停7 → 停8   （3区間目＝到達しないはず）
        },
        route_label={900: "3", 901: "2", 902: "1"},
    )
    boardings = {r["boarding_stop_id"] for r in rows}
    assert boardings == {9, 8}       # 停7 は含まれない



def test_reach_tiebreak_prefers_more_trips():
    """total が同点・transfer も同じなら、便数が多い経路を採る（2026-07-26 追加）。"""
    rows, _t = rc.build_reach(
        store_stops={100: [(1, 5), (2, 5)]},   # 降車停が2つ・徒歩はどちらも5分
        seg_by_alight={
            1: [(50, 10, 900, 1)],   # 停50 →10分→ 停1（1便）
            2: [(50, 10, 901, 9)],   # 停50 →10分→ 停2（9便）… total は同じ
        },
        route_label={900: "少ない方", 901: "多い方"},
    )
    assert len(rows) == 1                       # (乗車停50, 店100) で1行に畳まれる
    assert rows[0]["min_trip_count"] == 9
    assert rows[0]["route_label"] == "多い方"


def test_reach_shortest_still_wins_over_trip_count():
    """便数の tie-break は同点時のみ。所要が短い方が常に優先される。"""
    rows, _t = rc.build_reach(
        store_stops={100: [(1, 5), (2, 5)]},
        seg_by_alight={
            1: [(50, 8, 900, 1)],    # 8分・1便 ← 短いので勝つ
            2: [(50, 20, 901, 40)],  # 20分・40便
        },
        route_label={900: "短い方", 901: "本数は多いが遅い"},
    )
    assert rows[0]["route_label"] == "短い方"
    assert rows[0]["min_trip_count"] == 1


def test_reach_direct_preferred_over_transfer_on_tie():
    """既存ルールの回帰確認：所要が同点なら直行が乗換に優先する。"""
    rows, _t = rc.build_reach(
        store_stops={100: [(1, 5)]},
        seg_by_alight={
            1: [(2, 10, 900, 5), (9, 4, 902, 5)],  # 直行(停2→10分) と 停9→停1(4分)
            9: [(2, 6, 901, 5)],                    # 停2 →6分→ 停9  ＝ 合計10分で同点
        },
        route_label={900: "直行", 901: "1本目", 902: "2本目"},
    )
    row = [r for r in rows if r["boarding_stop_id"] == 2][0]
    assert row["transfer"] == "none"


def test_transfer_penalty_lets_slower_direct_win():
    """乗換ペナルティ：わずかに速いだけの乗換は直行に負ける。

    直行22分 vs 乗換20分 → 20 + TRANSFER_PENALTY_MIN(3) = 23 > 22 で直行が残る。
    ペナルティが無いと乗換が勝ってしまい、待ち時間を捨象している本アプリでは実態とズレる。
    """
    assert rc.TRANSFER_PENALTY_MIN == 3   # 前提が変わったらこのテストの数値も見直す
    rows, _t = rc.build_reach(
        store_stops={100: [(1, 0)]},
        seg_by_alight={
            1: [(2, 22, 900, 5), (9, 12, 902, 5)],  # 直行(停2→22分) と 停9→停1(12分)
            9: [(2, 8, 901, 5)],                     # 停2 →8分→ 停9 ＝ 乗換なら20分
        },
        route_label={900: "直行22分", 901: "乗換20分"},
    )
    row = [r for r in rows if r["boarding_stop_id"] == 2][0]
    assert row["transfer"] == "none" and row["ride_min"] == 22


def test_transfer_penalty_keeps_transfer_only_routes():
    """直行が存在しない経路は、ペナルティを足しても乗換のまま残る（店を落とさない）。"""
    rows, _t = rc.build_reach(
        store_stops={100: [(1, 0)]},
        seg_by_alight={
            1: [(9, 12, 902, 5)],    # 停1 へ来るのは停9 からだけ
            9: [(2, 8, 901, 5)],     # 停2 →8分→ 停9
        },
        route_label={901: "1本目", 902: "2本目"},
    )
    row = [r for r in rows if r["boarding_stop_id"] == 2][0]
    assert row["transfer"] == "hub1" and row["ride_min"] == 20  # 実値。ペナルティは含まない


def main():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok: {t.__name__}")
    print(f"全 {len(tests)} テスト成功")


if __name__ == "__main__":
    main()
