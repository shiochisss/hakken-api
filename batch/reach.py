"""reach（到達テーブル＝検索の心臓）生成バッチ。

各店に「どのバス停(乗車停)から・何分で・どの経路種別で到達できるか」を事前計算する。
検索(F4)は現在地の最寄り停で `reach WHERE boarding_stop_id IN(...)` を引く。

確定方針（2026-07-23）:
  - 経路: (1)直行=乗換なし（乗車停→店近傍停の**1区間**） (2)hub経由1回（乗車停→
    **is_hub停**→店近傍停の**2区間**）。hubは stops.is_hub=true のみ（DB設計書4-2）。
  - 到達時間 = 乗車分(route_segments.ride_min の経路合計) + 徒歩分(store_stops.walk2_min)。
    **待ち時間・乗換時間は捨象**（FSメモ・壁打ちログ20章）。walk1(現在地→乗車停)は検索時に加算。
  - 一意化: **(boarding_stop_id, store_id) ごとに最短(ride+walk2)の1行**に畳む
    （同一店×乗車停に複数経路があれば最短だけ残し、その transfer を保持）。
    同点の優先順は **直行 > hub経由 → 便数(min_trip_count)が多い方**（2026-07-26 追加）。
    所要が同じなら本数が多い経路の方が実用的なため。
  - 全量再生成（DELETE→INSERT・設計原則2）。

reach列（schema）: boarding_stop_id, store_id, ride_min, walk2_min, transfer(none/hub1),
  via_hub_id(hub1のみ), alight_stop_id(降車=店近傍停), route_label(乗車路線・hub1は1本目),
  min_trip_count(経路の便数・hub経由は2区間の最小値・NULL可)。
  id=IDENTITY / generated_at=now() は自動。

探索の起点は store_stops（店の近傍停＝降車停候補）。route_segments を
「直行=1区間」「hub経由=2区間(中間is_hub)」の**1〜2ホップ**辿る。
※route_segments は 2026-07-26 に「隣接停ペア」から「同一便の下流全停ペア」へ変更済み
  （9章#16）。1区間で複数停ぶん乗れるため、ホップ数は2のままでも実用的な経路が出る。

実行:
  python -m batch.reach --dry-run   # 読取＋算出＋サマリのみ（書込なし）
  python -m batch.reach             # 本反映（DELETE→INSERT）
必要な環境変数: DATABASE_URL（起動時に接続先[本番]/[ローカル]を表示＝誤投入防止）。
"""
from __future__ import annotations

import argparse
from collections import defaultdict

# しきい値の定義元は route_segments（1箇所に置く方針）。ここではサマリ表示にのみ使う。
from batch.route_segments import FEW_TRIPS_THRESHOLD

INSERT_CHUNK = 500


# ============================================================
# 純関数（DB非依存・テスト対象）
# ============================================================

def build_reach(store_stops: dict, seg_by_alight: dict, hubs: set, route_label: dict) -> tuple[list[dict], dict]:
    """reach 行を生成して (rows, tally) を返す。

    store_stops:   {store_id: [(alight_stop_id, walk2_min), ...]}  店の近傍停(降車候補)
    seg_by_alight: {alight_stop_id: [(boarding_stop_id, ride_min, route_id, trip_count), ...]}  1区間
    hubs:          {stop_id, ...}  is_hub=true の停
    route_label:   {route_id: label}
    """
    best: dict[tuple[int, int], dict] = {}  # (boarding, store) -> 最短行
    tally = {k: 0 for k in ("stores", "direct_cand", "hub_cand", "rows", "direct_rows", "hub_rows", "few_rows")}
    tally["stores"] = len(store_stops)

    def consider(boarding, store, alight, ride, walk2, transfer, via_hub, route_id, trips):
        if boarding == alight:      # 乗車停＝降車停は無意味（乗らない）
            return
        total = ride + walk2
        key = (boarding, store)
        cur = best.get(key)
        # 優先順（小さいほど良い）: ①最短(ride+walk2) ②直行 ③便数が多い
        # ③は 2026-07-26 追加。所要が同じなら本数が多い経路の方が実用的なため。
        rank = (total, 0 if transfer == "none" else 1, -trips)
        if cur is None or rank < cur["_rank"]:
            best[key] = {
                "boarding_stop_id": boarding, "store_id": store,
                "ride_min": ride, "walk2_min": walk2, "transfer": transfer,
                "via_hub_id": via_hub, "alight_stop_id": alight,
                "route_label": (route_label.get(route_id) or "?"),
                "min_trip_count": trips,
                "_rank": rank,
            }

    for store, alights in store_stops.items():
        for alight, walk2 in alights:
            ups = seg_by_alight.get(alight, ())
            # (1) 直行: B →[1区間]→ alight
            for b, ride1, rt1, tc1 in ups:
                tally["direct_cand"] += 1
                consider(b, store, alight, ride1, walk2, "none", None, rt1, tc1)
            # (2) hub経由: B →[1区間]→ H(is_hub) →[1区間]→ alight
            for h, ride2, _rt2, tc2 in ups:
                if h not in hubs:
                    continue
                for b, ride1, rt1, tc1 in seg_by_alight.get(h, ()):
                    if b == h or b == alight:
                        continue
                    tally["hub_cand"] += 1
                    # route_label=1本目(B→H)。便数は2区間の少ない方＝経路全体のボトルネック。
                    consider(b, store, alight, ride1 + ride2, walk2, "hub1", h, rt1, min(tc1, tc2))

    rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in best.values()]
    tally["rows"] = len(rows)
    tally["direct_rows"] = sum(1 for r in best.values() if r["transfer"] == "none")
    tally["hub_rows"] = sum(1 for r in best.values() if r["transfer"] == "hub1")
    tally["few_rows"] = sum(1 for r in best.values() if r["min_trip_count"] < FEW_TRIPS_THRESHOLD)
    return rows, tally


# ============================================================
# サマリ
# ============================================================

def _print_summary(tally: dict, rows: list[dict], db_stats: dict | None) -> None:
    print("== サマリ（reach）==")
    print("経路: 直行(1区間) + hub経由1回(2区間・中間はis_hub停)。到達時間=乗車+徒歩(待ち捨象)")
    print(f"入力: 対象店 {tally['stores']} 店")
    print(f"生成: reach {tally['rows']} 行（直行 {tally['direct_rows']} / hub経由 {tally['hub_rows']}）")
    if tally["rows"]:
        pct = tally["few_rows"] / tally["rows"] * 100
        print(f"「本数少なめ」(min_trip_count<{FEW_TRIPS_THRESHOLD}): {tally['few_rows']} 行（{pct:.1f}%）")
    print(f"候補数(集約前): 直行 {tally['direct_cand']} / hub {tally['hub_cand']}")
    if rows:
        rides = [r["ride_min"] for r in rows]
        totals = [r["ride_min"] + r["walk2_min"] for r in rows]
        print(f"乗車分 ride_min: 最小{min(rides)} / 最大{max(rides)} 分")
        print(f"到達(乗車+徒歩walk2): 最小{min(totals)} / 最大{max(totals)} 分  ※walk1(現在地→乗車停)は検索時加算")
    if db_stats is None:
        print("DB反映: （ドライラン＝書込なし。DELETE→INSERTは本反映時に実行）")
    else:
        print(f"DB反映: DELETE後 INSERT={db_stats['inserted']} 行")


# ============================================================
# DB接続（begin() で1トランザクション・全量再生成）
# ============================================================

_INSERT_SQL = (
    "INSERT INTO reach "
    "(boarding_stop_id, store_id, ride_min, walk2_min, transfer, via_hub_id, alight_stop_id, "
    " route_label, min_trip_count) "
    "VALUES (:boarding_stop_id, :store_id, :ride_min, :walk2_min, :transfer, :via_hub_id, "
    "        :alight_stop_id, :route_label, :min_trip_count)"
)


def main(dry_run: bool = False) -> None:
    from sqlalchemy import text
    from batch.db import begin, log_connection_target

    log_connection_target()  # 起動時に接続先（本番/ローカル）を表示＝誤投入防止

    db_stats = None
    with begin() as conn:  # 接続失敗時はパスワードを伏せて再送出（batch/db.py）
        store_stops: dict[int, list] = defaultdict(list)
        for r in conn.execute(text("SELECT store_id, stop_id, walk2_min FROM store_stops")):
            store_stops[r.store_id].append((r.stop_id, r.walk2_min))
        seg_by_alight: dict[int, list] = defaultdict(list)
        for r in conn.execute(text(
            "SELECT route_id, boarding_stop_id, alight_stop_id, ride_min, trip_count FROM route_segments"
        )):
            seg_by_alight[r.alight_stop_id].append((r.boarding_stop_id, r.ride_min, r.route_id, r.trip_count))
        hubs = {r.id for r in conn.execute(text("SELECT id FROM stops WHERE is_hub = true"))}
        route_label = {r.id: r.label for r in conn.execute(text("SELECT id, label FROM routes"))}

        rows, tally = build_reach(store_stops, seg_by_alight, hubs, route_label)

        if not dry_run:
            conn.execute(text("DELETE FROM reach"))  # 全量再生成（設計原則2）
            for i in range(0, len(rows), INSERT_CHUNK):
                conn.execute(text(_INSERT_SQL), rows[i:i + INSERT_CHUNK])
            db_stats = {"inserted": len(rows)}

    _print_summary(tally, rows, db_stats)
    if not dry_run:
        print("完了")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="reach（到達テーブル）生成バッチ")
    ap.add_argument("--dry-run", action="store_true", help="書込なしで読取〜算出〜サマリのみ")
    args = ap.parse_args()
    main(dry_run=args.dry_run)
