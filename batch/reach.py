"""reach（到達テーブル＝検索の心臓）生成バッチ。

各店に「どのバス停(乗車停)から・何分で・どの経路種別で到達できるか」を事前計算する。
検索(F4)は現在地の最寄り停で `reach WHERE boarding_stop_id IN(...)` を引く。

確定方針（2026-07-23 制定・2026-07-26 改訂）:
  - 経路: (1)直行=乗換なし（乗車停→店近傍停の**1区間**） (2)**乗換1回**（乗車停→
    乗換停→店近傍停の**2区間**）。**乗換停は任意の停**でよい。
  - 到達時間 = 乗車分(route_segments.ride_min の経路合計) + 徒歩分(store_stops.walk2_min)。
    **待ち時間・乗換時間は捨象**（FSメモ・壁打ちログ20章）。walk1(現在地→乗車停)は検索時に加算。
  - 一意化: **(boarding_stop_id, store_id) ごとに1行**に畳む。優先順は
    **①ペナルティ込みの最短 ②直行 ③便数(min_trip_count)が多い方**。
  - 全量再生成（DELETE→INSERT・設計原則2）。

【2026-07-26 改訂・DB設計書 9章#16】乗換停の `is_hub` 限定をやめた。
  ホワイトリスト（`練馬駅`/`光が丘`/`江古田駅` の名前一致・49停）が狭すぎて
  **hub経由は到達できる乗車停を1停も増やしていなかった**（直行のみ713停／hub込みでも713停）。
  任意の停で乗り換えられるようにすると 1,200停になり、江古田駅起点・歩かない条件で
  ヒットが3店→8店に増える。`stops.is_hub` は残るが**本バッチはもう参照しない**。
  乗換2回（3区間）は見送り＝待ち時間を捨象したモデルでは実態との乖離が大きいため。

reach列（schema）: boarding_stop_id, store_id, ride_min, walk2_min, transfer(none/hub1),
  via_hub_id(乗換停・hub1のみ), alight_stop_id(降車=店近傍停),
  route_label(乗車路線・hub1は1本目), min_trip_count(経路の便数・hub1は2区間の最小値・NULL可)。
  id=IDENTITY / generated_at=now() は自動。
  ※`transfer` の値 `hub1` は「**乗換1回**」の意味（乗換停がハブとは限らない）。改名すると
    本番の CHECK 制約・API・フロントの型を同時に変える必要があるため値名は据え置いた。

探索は store_stops（店の近傍停＝降車停候補）を起点に、route_segments を**後ろ向きに**
1区間ずつ緩和する（層0=降車停 → 層1=直行 → 層2=乗換1回）。
※route_segments は 2026-07-26 に「隣接停ペア」から「同一便の下流全停ペア」へ変更済み
  （9章#16）。1区間で複数停ぶん乗れるため、2区間でも実用的な経路が出る。

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

# ★★★ 乗換1回あたりのペナルティは暫定で 3分（2026-07-26 ibes 判断）。★★★
#   本アプリは待ち時間・乗換時間を捨象している（FSメモ・壁打ちログ20章）。そのままだと
#   「1分早いだけの乗換」が直行に勝ってしまうため、**経路の選択にだけ**加算する。
#   ride_min には含めない＝画面に出る時間は実値のまま（表示を水増ししない）。
#   実測では +3分 でどの起点もヒット店数を減らさずに無駄な乗換だけが消えた
#   （+5分にすると石神井公園/バランスが5店→4店に減る＝乗換でしか行けない店を落とし始める）。
TRANSFER_PENALTY_MIN = 3

# 探索する最大区間数。2 = 直行(1区間) と 乗換1回(2区間)。
MAX_SEGMENTS = 2


# ============================================================
# 純関数（DB非依存・テスト対象）
# ============================================================

def _rank(ride: int, walk2: int, n_transfer: int, trips: int) -> tuple:
    """経路の良さ（小さいほど良い）。

    ①ペナルティ込みの所要 ②直行を優先 ③便数が多い方を優先。
    ペナルティはここ（＝選択）にだけ効き、保存する ride_min は実値のまま。
    """
    return (ride + walk2 + TRANSFER_PENALTY_MIN * n_transfer, n_transfer, -trips)


def build_reach(store_stops: dict, seg_by_alight: dict, route_label: dict) -> tuple[list[dict], dict]:
    """reach 行を生成して (rows, tally) を返す。

    store_stops:   {store_id: [(alight_stop_id, walk2_min), ...]}  店の近傍停(降車候補)
    seg_by_alight: {alight_stop_id: [(boarding_stop_id, ride_min, route_id, trip_count), ...]}  1区間
    route_label:   {route_id: label}

    降車停から**後ろ向きに**区間を辿る層状の探索。層 k は「k 区間乗れば店に着ける停」の集合で、
    層1が直行・層2が乗換1回にあたる（MAX_SEGMENTS まで）。各層で停ごとに最良の1件だけを
    残して次層へ渡す。コストが非負なので、これで最適解が得られる（層ごとの緩和＝
    Bellman-Ford と同じ理屈）。中継点に制限は無い＝**任意の停で乗り換えられる**。
    """
    best: dict[tuple[int, int], dict] = {}  # (boarding, store) -> 採用した1行
    tally = {k: 0 for k in ("stores", "cand", "rows", "direct_rows", "transfer_rows", "few_rows")}
    tally["stores"] = len(store_stops)

    for store, alights in store_stops.items():
        # 層0: 店の近傍停。まだバスに乗っていないので ride=0。
        #   frontier[stop] = (ride, walk2, alight, route_id, trips, rank)
        frontier: dict[int, tuple] = {}
        for alight, walk2 in alights:
            cand = (0, walk2, alight, None, None, _rank(0, walk2, 0, 0))
            cur = frontier.get(alight)
            if cur is None or cand[5] < cur[5]:
                frontier[alight] = cand

        for depth in range(MAX_SEGMENTS):
            n_transfer = depth          # 層1(depth=0)は乗換0＝直行、層2(depth=1)は乗換1回
            transfer = "none" if n_transfer == 0 else "hub1"
            nxt: dict[int, tuple] = {}
            for v, (ride_v, walk2, alight, rt_v, trips_v, _rk) in frontier.items():
                for b, ride1, rt1, tc1 in seg_by_alight.get(v, ()):
                    if b == v or b == alight:   # 乗車停＝乗換停／降車停は無意味（乗らない）
                        continue
                    ride = ride_v + ride1
                    # 便数は経路中の最小＝ボトルネック。route_label は必ず1本目（＝いま乗る便）。
                    trips = tc1 if trips_v is None else min(tc1, trips_v)
                    rk = _rank(ride, walk2, n_transfer, trips)
                    tally["cand"] += 1
                    cur = nxt.get(b)
                    if cur is None or rk < cur[5]:
                        # 次層では b が「乗換停」になる。alight/walk2 は店側のまま持ち回す。
                        nxt[b] = (ride, walk2, alight, rt1, trips, rk)
                    key = (b, store)
                    prev = best.get(key)
                    if prev is None or rk < prev["_rank"]:
                        best[key] = {
                            "boarding_stop_id": b, "store_id": store,
                            "ride_min": ride, "walk2_min": walk2, "transfer": transfer,
                            "via_hub_id": v if n_transfer else None,
                            "alight_stop_id": alight,
                            "route_label": (route_label.get(rt1) or "?"),
                            "min_trip_count": trips,
                            "_rank": rk,
                        }
            frontier = nxt
            if not frontier:
                break

    rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in best.values()]
    tally["rows"] = len(rows)
    tally["direct_rows"] = sum(1 for r in best.values() if r["transfer"] == "none")
    tally["transfer_rows"] = sum(1 for r in best.values() if r["transfer"] == "hub1")
    tally["few_rows"] = sum(1 for r in best.values() if r["min_trip_count"] < FEW_TRIPS_THRESHOLD)
    return rows, tally


# ============================================================
# サマリ
# ============================================================

def _print_summary(tally: dict, rows: list[dict], db_stats: dict | None) -> None:
    print("== サマリ（reach）==")
    print("経路: 直行(1区間) + 乗換1回(2区間・乗換停は任意)。到達時間=乗車+徒歩(待ち捨象)")
    print(f"経路選択の乗換ペナルティ = +{TRANSFER_PENALTY_MIN}分/回 ※暫定・ride_min には含めない")
    print(f"入力: 対象店 {tally['stores']} 店")
    if tally["rows"]:
        tpct = tally["transfer_rows"] / tally["rows"] * 100
        print(f"生成: reach {tally['rows']} 行"
              f"（直行 {tally['direct_rows']} / 乗換1回 {tally['transfer_rows']}＝{tpct:.1f}%）")
        pct = tally["few_rows"] / tally["rows"] * 100
        print(f"「本数少なめ」(min_trip_count<{FEW_TRIPS_THRESHOLD}): {tally['few_rows']} 行（{pct:.1f}%）")
    else:
        print("生成: reach 0 行")
    print(f"候補数(集約前): {tally['cand']}")
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
        # ※stops.is_hub はもう読まない（2026-07-26・冒頭の改訂メモ参照）。乗換停は任意の停。
        route_label = {r.id: r.label for r in conn.execute(text("SELECT id, label FROM routes"))}

        rows, tally = build_reach(store_stops, seg_by_alight, route_label)

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
