"""store_stops（店↔近傍バス停の紐付け）生成バッチ。

reach の前提となる walk2（降車停→店の徒歩分）を事前計算する。
F9 / route_segments / stores_import と同じ設計思想（接続先表示 → 読取 → 算出 →
UPSERT → stale削除 → dry-run 対応 → サマリ。begin() で1トランザクション）。

確定仕様（DB設計書 v1.7 / 9章#6・4-5）:
  - 保持数 K = 各店 **徒歩10分圏内の全停・ただし上限20停**。
  - 徒歩分 = 直線距離(haversine) × 1.3 ÷ 80m/分（FS実測誤差0.6分）。
  - 出力 store_stops(store_id, stop_id, walk2_min, distance_m)。PK=(store_id, stop_id)。
  - 全店ぶんを1トランザクションで UPSERT ＋ 今回未生成キーを stale削除（全量再生成）。

入力（DBから読む）: stores(id, lat, lng) / stops(id, lat, lng)。
  ※入力がDBのサロゲートIDのため、dry-run でも DB の SELECT は行う（書込のみ抑止）。

実行:
  python -m batch.store_stops --dry-run   # 読取＋算出＋サマリのみ（書込なし）
  python -m batch.store_stops             # 本反映（UPSERT＋stale削除）
必要な環境変数: DATABASE_URL（hakken-api/.env。起動時に接続先を表示＝誤投入防止）。
"""
from __future__ import annotations

import argparse
import math

# ============================================================
# 設定値（DB設計書 v1.7・9章#6。勝手に変えない）
# ============================================================
WALK_LIMIT_MIN = 10          # 徒歩上限（分）＝10分圏
MAX_STOPS_PER_STORE = 20     # 1店あたりの保持上限（近い順）
WALK_DETOUR = 1.3            # 直線→道のり補正係数
WALK_SPEED_M_PER_MIN = 80    # 徒歩速度（m/分）
UPSERT_CHUNK = 500


# ============================================================
# 純関数（DB非依存・テスト対象）
# ============================================================

def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """2点間の大圏距離（メートル）。"""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def walk_min(distance_m: float) -> float:
    """直線距離(m) → 徒歩分（直線×1.3÷80m/分）。丸めない生の分。"""
    return distance_m * WALK_DETOUR / WALK_SPEED_M_PER_MIN


def build_store_stops(stores: list[tuple], stops: list[tuple]) -> tuple[list[dict], dict]:
    """各店について徒歩10分圏内の停を近い順に最大20停抽出して行を作る。

    stores/stops: [(id, lat, lng), ...]。
    返り値: (rows, tally)。rows=UPSERT用dict、tally=集計。
    """
    rows: list[dict] = []
    tally = {
        "stores": len(stores),
        "stops": len(stops),
        "stores_with_stops": 0,
        "stores_zero": 0,      # 10分圏内に停が無かった店
        "capped": 0,           # 20停上限で足切りされた店
        "pairs": 0,
    }
    for sid, slat, slng in stores:
        cands: list[tuple[float, int, float]] = []  # (distance_m, stop_id, walk_min)
        for pid, plat, plng in stops:
            d = haversine_m(slat, slng, plat, plng)
            wm = walk_min(d)
            if wm <= WALK_LIMIT_MIN:
                cands.append((d, pid, wm))
        cands.sort(key=lambda x: x[0])  # 距離の近い順
        if not cands:
            tally["stores_zero"] += 1
            continue
        if len(cands) > MAX_STOPS_PER_STORE:
            tally["capped"] += 1
        kept = cands[:MAX_STOPS_PER_STORE]
        tally["stores_with_stops"] += 1
        for d, pid, wm in kept:
            rows.append({
                "store_id": sid,
                "stop_id": pid,
                "walk2_min": int(round(wm)),
                "distance_m": int(round(d)),
            })
    tally["pairs"] = len(rows)
    return rows, tally


# ============================================================
# サマリ
# ============================================================

def _print_summary(tally: dict, db_stats: dict | None) -> None:
    print("== サマリ（store_stops）==")
    print(f"K = 徒歩{WALK_LIMIT_MIN}分圏・上限{MAX_STOPS_PER_STORE}停（直線×{WALK_DETOUR}÷{WALK_SPEED_M_PER_MIN}m/分・DB設計書v1.7）")
    print(f"入力: stores {tally['stores']} 件 / stops {tally['stops']} 件")
    print(f"生成: store_stops {tally['pairs']} 行"
          f"（近傍あり {tally['stores_with_stops']} 店 / 近傍0 {tally['stores_zero']} 店 / 20停上限で足切り {tally['capped']} 店）")
    if db_stats is None:
        print("DB反映: （ドライラン＝書込なし。UPSERT/削除は本反映時に実行）")
    else:
        print(f"DB反映: UPSERT={db_stats['upsert']} / stale削除={db_stats['deleted']}")


# ============================================================
# DB接続を伴う部分（begin() で1トランザクション）
# ============================================================

def _store_stops_table(md):
    from sqlalchemy import BigInteger, Column, Integer, Table
    return Table(
        "store_stops", md,
        Column("store_id", BigInteger, primary_key=True),
        Column("stop_id", BigInteger, primary_key=True),
        Column("walk2_min", Integer),
        Column("distance_m", Integer),
    )


def _upsert(conn, tbl, rows: list[dict]) -> None:
    from sqlalchemy.dialects.postgresql import insert
    stmt = insert(tbl).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[tbl.c.store_id, tbl.c.stop_id],
        set_={"walk2_min": stmt.excluded.walk2_min, "distance_m": stmt.excluded.distance_m},
    )
    conn.execute(stmt)


def _delete_stale(conn, tbl, seen_keys: list[tuple]) -> int:
    """今回生成しなかった (store_id, stop_id) を削除（全量再生成）。"""
    from sqlalchemy import delete, text, tuple_
    if not seen_keys:
        return conn.execute(text("DELETE FROM store_stops")).rowcount or 0
    stmt = delete(tbl).where(
        tuple_(tbl.c.store_id, tbl.c.stop_id).notin_(seen_keys)
    )
    return conn.execute(stmt).rowcount or 0


def main(dry_run: bool = False) -> None:
    from sqlalchemy import MetaData, text
    from batch.db import begin, log_connection_target

    log_connection_target()  # 起動時に接続先（本番/ローカル）を表示＝誤投入防止

    db_stats = None
    with begin() as conn:  # 接続失敗時はパスワードを伏せて再送出（batch/db.py）
        stores = [(r.id, r.lat, r.lng) for r in conn.execute(text("SELECT id, lat, lng FROM stores"))]
        stops = [(r.id, r.lat, r.lng) for r in conn.execute(text("SELECT id, lat, lng FROM stops"))]
        rows, tally = build_store_stops(stores, stops)

        if not dry_run:
            md = MetaData()
            tbl = _store_stops_table(md)
            for i in range(0, len(rows), UPSERT_CHUNK):
                _upsert(conn, tbl, rows[i:i + UPSERT_CHUNK])
            seen_keys = [(r["store_id"], r["stop_id"]) for r in rows]
            deleted = _delete_stale(conn, tbl, seen_keys)
            db_stats = {"upsert": len(rows), "deleted": deleted}

    _print_summary(tally, db_stats)
    if not dry_run:
        print("完了")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="store_stops（店↔近傍バス停）生成バッチ")
    ap.add_argument("--dry-run", action="store_true", help="書込なしで読取〜算出〜サマリのみ")
    args = ap.parse_args()
    main(dry_run=args.dry_run)
