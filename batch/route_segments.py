"""F9（続き）: GTFS-JP から routes / route_segments を生成する夜間バッチ。

reach.db 生成の前提となる「区間乗車時間」を作る。f9_stops.py（stops 取込）と
同じ設計思想（GTFS zip 取得 → パース → UPSERT → stale削除 → dry-run 対応）。

処理の流れ:
  1) 各社の GTFS zip を取得（f9_stops と同じ gtfs_reader.download。1社失敗はスキップ）。
     ※ 同じ zip から routes.txt / trips.txt / stop_times.txt / calendar.txt を
       まとめて読む（方針: routes 取込を route_segments バッチに同梱＝案B）。
  2) パース＋区間算出（DB非依存・テスト対象）:
       - calendar.txt を正として「土日に運行する service_id」を抽出（#7-a）。
         calendar_dates.txt（祝日等の例外）は今回見送り。
       - trips.txt で trip_id → (route_id, service_id) を引く。
       - stop_times.txt を trip 単位・stop_sequence 順に並べ、**隣接停ペア**のみ
         区間化（#7-c: reach 側が hub 経由で複数区間を組むため細粒度に保つ）。
       - 「昼」の時間帯（下記 LUNCH_*）に乗車停を出発する便のみ採用（#7-b・暫定）。
       - 同一 (route, 乗車停, 降車停) に集まった複数便の所要秒の **中央値** を代表値に（#7-d）。
       - 24時超え表記（25:10:00 等）は総秒に換算し、時間帯判定は %86400（翌日扱い）。
         負値・0分・欠損時刻はスキップ（#7-e）。
  3) DB反映（1トランザクション）: routes を gtfs_route_id で UPSERT → route_segments を
     複合PKで UPSERT → 未生成の区間/路線を stale 削除。DDLは流さない。
  4) サマリ出力（dry-run では DB書込なし）。

方針の根拠:
  - DB設計書 4-4 / 9章#7（代表値の決め方は F9 実装の論点＝TBD）
  - 要件定義書 v1.4 para138（区間所要時間を自動生成する夜間バッチ）
  - schema_postgres.sql: routes(id/gtfs_route_id/label/operator),
    route_segments(route_id, boarding_stop_id, alight_stop_id, ride_min; 複合PK)

実行: (.venv 有効化後)
  python -m batch.route_segments --dry-run   # DB書込なしで取得〜算出〜サマリ
  python -m batch.route_segments             # 本反映
必要な環境変数（hakken-api/.env）: DATABASE_URL / ODPT_CONSUMER_KEY
"""
from __future__ import annotations

import argparse
import statistics
from collections import defaultdict
from pathlib import Path

from batch import gtfs_reader as gtfs

# ============================================================
# 設定値（マジックナンバーをロジックに埋め込まず、ここで一元管理する）
# ============================================================

# ★★★ 「昼」の時間帯は暫定で 10:00-16:00 と仮決め（2026-07-22）。★★★
#   ユーザー向けの「楽さ」数値（ride_min）に直結するプロダクト判断のため、
#   しおちさんに要共有・要確認。DB設計書 9章#7-b の TBD 項目。
#   確定したら下記2定数を差し替える（dry-run サマリにも毎回表示される）。
LUNCH_START_SEC = 10 * 3600   # 10:00
LUNCH_END_SEC = 16 * 3600     # 16:00（未満）
LUNCH_LABEL = "10:00-16:00 ※暫定・要共有(9章#7-b)"

# 土日判定は calendar.txt を正とする（#7-a）。calendar_dates.txt の例外（祝日等）は今回見送り。
# 【既知の制限】土日判定は calendar.txt 方式。calendar_dates.txt のみの
#   事業者（小田急/odakyu）は土日集合が空になり route_segments が0件になる。
#   影響は 8停のみ・豊玉エリア無関係のため後回し（MTG確定 2026-07-22）。
#   対応時は calendar_dates.txt 方式の分岐を追加する。
WEEKEND_COLS = ("saturday", "sunday")

# 列長ガード（schema_postgres.sql: gtfs_route_id VARCHAR(128) / label VARCHAR(255) / operator VARCHAR(128)）
MAX_GTFS_ROUTE_ID = 128
MAX_ROUTE_LABEL = 255
MAX_OPERATOR = 128

UPSERT_CHUNK = 500

# tally（集計）キー
_TALLY_KEYS = (
    "weekend_services",  # calendar.txt で土日運行と判定した service_id 数
    "trips_total",       # trips.txt の便数
    "trip_weekend",      # うち土日便として採用した数
    "skip_no_trip",      # stop_times にあるが trips.txt に無い trip
    "skip_not_weekend",  # 土日でない便
    "skip_not_lunch",    # 乗車停出発が昼の時間帯外の隣接ペア
    "skip_time_bad",     # 時刻パース不可の隣接ペア
    "skip_nonpos",       # 所要が負値・0秒の隣接ペア
    "skip_no_stop_id",   # stop_id 欠損の隣接ペア
    "skip_zero_min",     # 中央値が分に丸めると0分になった区間
    "pair_kept",         # 採用した隣接ペア（集約前）
    "segments",          # 集約後の区間数（route,乗車,降車 のユニーク）
)


# ============================================================
# 純関数（DB非依存・テスト対象）
# ============================================================

def parse_gtfs_time(s: str | None) -> int | None:
    """GTFS の HH:MM:SS を「サービス開始からの総秒」で返す。24時超え（25:10:00 等）も
    そのまま総秒に換算する。不正・空は None（#7-e: 欠損はスキップ）。"""
    if not s:
        return None
    parts = s.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, sec = (int(p) for p in parts)
    except ValueError:
        return None
    if h < 0 or not (0 <= m <= 59) or not (0 <= sec <= 59):
        return None
    return h * 3600 + m * 60 + sec


def in_lunch_window(total_sec: int) -> bool:
    """総秒の「時刻（0-86399）」が昼の時間帯に入るか。24時超えは %86400 で翌日扱い（#7-e）。"""
    tod = total_sec % 86400
    return LUNCH_START_SEC <= tod < LUNCH_END_SEC


def weekend_service_ids(cal_rows) -> set[str]:
    """calendar.txt の行から土日運行の service_id 集合を返す（#7-a）。"""
    out: set[str] = set()
    for r in cal_rows:
        sid = (r.get("service_id") or "").strip()
        if not sid:
            continue
        if any((r.get(c) or "").strip() == "1" for c in WEEKEND_COLS):
            out.add(sid)
    return out


def load_trips(trip_rows) -> dict[str, tuple[str, str]]:
    """trips.txt → {trip_id: (route_id, service_id)}。"""
    trips: dict[str, tuple[str, str]] = {}
    for r in trip_rows:
        tid = (r.get("trip_id") or "").strip()
        rid = (r.get("route_id") or "").strip()
        sid = (r.get("service_id") or "").strip()
        if tid:
            trips[tid] = (rid, sid)
    return trips


def load_routes(route_rows) -> dict[str, str]:
    """routes.txt → {route_id: label}。label は long_name 優先、無ければ short_name。"""
    out: dict[str, str] = {}
    for r in route_rows:
        rid = (r.get("route_id") or "").strip()
        if not rid:
            continue
        label = (r.get("route_long_name") or r.get("route_short_name") or "").strip()
        out[rid] = label
    return out


def _stop_times_by_trip(st_rows) -> dict[str, list[dict]]:
    """stop_times.txt を trip_id ごとにまとめる。"""
    by_trip: dict[str, list[dict]] = defaultdict(list)
    for r in st_rows:
        tid = (r.get("trip_id") or "").strip()
        if tid:
            by_trip[tid].append(r)
    return by_trip


def _seq_key(row: dict):
    """stop_sequence を整数として並べる（不正は文字列フォールバック）。"""
    v = (row.get("stop_sequence") or "").strip()
    try:
        return (0, int(v))
    except ValueError:
        return (1, v)


def build_segments(operator: str, st_rows, trips: dict, weekend: set[str], tally: dict) -> dict:
    """隣接停ペアの所要秒を集めて中央値で集約し、{(route_gid, 乗車gid, 降車gid): ride_min} を返す。

    - gtfs_stop_id / gtfs_route_id は f9_stops と同じ "operator:原ID" 規則で付与し一貫させる。
    - 昼フィルタは乗車停の出発時刻（無ければ到着）で判定。
    - ride_min = 降車停の到着 − 乗車停の出発（秒）→ 中央値 → 分へ丸め。
    """
    acc: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for tid, rows in _stop_times_by_trip(st_rows).items():
        meta = trips.get(tid)
        if not meta:
            tally["skip_no_trip"] += 1
            continue
        rid, sid = meta
        if sid not in weekend:
            tally["skip_not_weekend"] += 1
            continue
        tally["trip_weekend"] += 1
        rows.sort(key=_seq_key)
        for a, b in zip(rows, rows[1:]):
            dep_a = parse_gtfs_time(a.get("departure_time") or a.get("arrival_time"))
            arr_b = parse_gtfs_time(b.get("arrival_time") or b.get("departure_time"))
            if dep_a is None or arr_b is None:
                tally["skip_time_bad"] += 1
                continue
            if not in_lunch_window(dep_a):
                tally["skip_not_lunch"] += 1
                continue
            ride = arr_b - dep_a
            if ride <= 0:  # 負値・0分は除外（#7-e）
                tally["skip_nonpos"] += 1
                continue
            sa = (a.get("stop_id") or "").strip()
            sb = (b.get("stop_id") or "").strip()
            if not sa or not sb:
                tally["skip_no_stop_id"] += 1
                continue
            key = (f"{operator}:{rid}", f"{operator}:{sa}", f"{operator}:{sb}")
            acc[key].append(ride)
            tally["pair_kept"] += 1

    segments: dict[tuple[str, str, str], int] = {}
    for key, rides in acc.items():
        ride_min = int(round(statistics.median(rides) / 60))
        if ride_min <= 0:  # 中央値が分に丸めて0分（超短区間）はスキップ
            tally["skip_zero_min"] += 1
            continue
        segments[key] = ride_min
    tally["segments"] = len(segments)
    return segments


def process_operator(operator: str, zip_path: Path) -> tuple[dict, dict, dict]:
    """1社分の zip をパースして (routes_map, segments, tally) を返す（DB非依存）。

    calendar.txt が無い社は土日判定不可のため空集合（全便が非土日扱い）＝警告のみ。
    """
    tally = {k: 0 for k in _TALLY_KEYS}
    try:
        cal_rows = list(gtfs.read_table(zip_path, "calendar.txt"))
    except RuntimeError:
        print(f"  [警告] {operator}: calendar.txt が無いため土日判定不可（区間0件で継続）")
        cal_rows = []
    weekend = weekend_service_ids(cal_rows)
    tally["weekend_services"] = len(weekend)

    routes_map = load_routes(gtfs.read_table(zip_path, "routes.txt"))
    trips = load_trips(gtfs.read_table(zip_path, "trips.txt"))
    tally["trips_total"] = len(trips)

    segments = build_segments(operator, gtfs.read_table(zip_path, "stop_times.txt"), trips, weekend, tally)
    return routes_map, segments, tally


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n]


# ============================================================
# サマリ出力
# ============================================================

def _print_summary(parsed: dict, failed: dict | None, db_stats: dict | None) -> None:
    print("== サマリ（route_segments）==")
    print(f"「昼」時間帯フィルタ = {LUNCH_LABEL}")  # #7-b が仮決めであることを毎回明示
    header = (
        f"{'社':<10}{'土日SVC':>7}{'便数':>7}{'土日便':>7}"
        f"{'採用対':>7}{'区間':>7}{'非土日':>7}{'圏外時':>7}{'時刻異':>7}{'非正':>6}"
    )
    print(header)
    total_seg = 0
    for operator, (_routes, _segs, t) in parsed.items():
        total_seg += t["segments"]
        print(
            f"{operator:<10}{t['weekend_services']:>7}{t['trips_total']:>7}{t['trip_weekend']:>7}"
            f"{t['pair_kept']:>7}{t['segments']:>7}{t['skip_not_weekend']:>7}"
            f"{t['skip_not_lunch']:>7}{t['skip_time_bad']:>7}{t['skip_nonpos']:>6}"
        )
    print(f"{'合計区間':<10}{total_seg:>21}")
    if failed:
        print(f"取得失敗: {len(failed)} 社 -> " + ", ".join(f"{op}({msg})" for op, msg in failed.items()))
    else:
        print("取得失敗: 0 社")
    if db_stats is None:
        print("DB反映: （ドライラン＝未反映。停・路線のサロゲートID紐付けとUPSERTは本反映時に実行）")
    else:
        print(
            f"DB反映: routes upsert={db_stats['routes_upsert']} / 削除={db_stats['routes_deleted']}"
            f"  route_segments upsert={db_stats['seg_upsert']} / 削除={db_stats['seg_deleted']}"
            f"  停未マッチ区間スキップ={db_stats['seg_skip_no_stop']}"
        )


# ============================================================
# DB接続を伴う部分（Azure Database for PostgreSQL）。--dry-run では呼ばれない。
# ============================================================

def _routes_table(md):
    from sqlalchemy import BigInteger, Column, String, Table
    return Table(
        "routes", md,
        Column("id", BigInteger, primary_key=True),
        Column("gtfs_route_id", String),
        Column("label", String),
        Column("operator", String),
    )


def _segments_table(md):
    from sqlalchemy import BigInteger, Column, Integer, Table
    return Table(
        "route_segments", md,
        Column("route_id", BigInteger, primary_key=True),
        Column("boarding_stop_id", BigInteger, primary_key=True),
        Column("alight_stop_id", BigInteger, primary_key=True),
        Column("ride_min", Integer),
    )


def _upsert_routes(conn, routes_tbl, rows: list[dict]) -> None:
    from sqlalchemy.dialects.postgresql import insert
    stmt = insert(routes_tbl).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[routes_tbl.c.gtfs_route_id],
        set_={"label": stmt.excluded.label, "operator": stmt.excluded.operator},
    )
    conn.execute(stmt)


def _upsert_segments(conn, seg_tbl, rows: list[dict]) -> None:
    from sqlalchemy.dialects.postgresql import insert
    stmt = insert(seg_tbl).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[seg_tbl.c.route_id, seg_tbl.c.boarding_stop_id, seg_tbl.c.alight_stop_id],
        set_={"ride_min": stmt.excluded.ride_min},
    )
    conn.execute(stmt)


def _delete_stale_segments(conn, seg_tbl, op_route_pks: list[int], seen_keys: list[tuple]) -> int:
    """この社の路線に属する route_segments のうち、今回生成しなかった複合キーを削除。"""
    from sqlalchemy import delete, tuple_
    if not op_route_pks:
        return 0
    stmt = delete(seg_tbl).where(seg_tbl.c.route_id.in_(op_route_pks))
    if seen_keys:
        stmt = stmt.where(
            tuple_(seg_tbl.c.route_id, seg_tbl.c.boarding_stop_id, seg_tbl.c.alight_stop_id).notin_(seen_keys)
        )
    return conn.execute(stmt).rowcount or 0


def _delete_stale_routes(conn, operator: str, seen_route_gids: set[str]) -> int:
    """この社の routes のうち、今回未生成かつ route_segments から未参照のものを削除（f9 と同思想）。"""
    from sqlalchemy import bindparam, text
    if not seen_route_gids:
        return 0
    stmt = text(
        """
        DELETE FROM routes r
        WHERE r.gtfs_route_id LIKE :prefix
          AND r.gtfs_route_id NOT IN :seen
          AND NOT EXISTS (SELECT 1 FROM route_segments rs WHERE rs.route_id = r.id)
        """
    ).bindparams(bindparam("seen", expanding=True))
    res = conn.execute(stmt, {"prefix": f"{operator}:%", "seen": list(seen_route_gids)})
    return res.rowcount or 0


def _apply_to_db(parsed: dict) -> dict:
    """routes / route_segments を1トランザクションで反映して統計を返す。"""
    from sqlalchemy import MetaData, text
    from batch.db import begin

    stats = {"routes_upsert": 0, "routes_deleted": 0, "seg_upsert": 0, "seg_deleted": 0, "seg_skip_no_stop": 0}
    md = MetaData()
    routes_tbl = _routes_table(md)
    seg_tbl = _segments_table(md)

    with begin() as conn:  # 接続失敗時はパスワードを伏せて再送出（batch/db.py）
        # 1) routes UPSERT（区間に現れた路線のみ＝FK被参照を保証）
        for operator, (routes_map, segments, _t) in parsed.items():
            seen_route_gids = {k[0] for k in segments}
            rows = []
            for gid in seen_route_gids:
                raw_rid = gid.split(":", 1)[1]
                if len(gid) > MAX_GTFS_ROUTE_ID:
                    continue  # ID長超はスキップ（切詰は衝突しうる）
                label = routes_map.get(raw_rid, "") or gid  # label は NOT NULL。空なら gid で代替
                rows.append({
                    "gtfs_route_id": gid,
                    "label": _truncate(label, MAX_ROUTE_LABEL),
                    "operator": _truncate(operator, MAX_OPERATOR),
                })
            for i in range(0, len(rows), UPSERT_CHUNK):
                _upsert_routes(conn, routes_tbl, rows[i:i + UPSERT_CHUNK])
            stats["routes_upsert"] += len(rows)

        # 2) サロゲートID解決用マップ（stops は F9 投入済み・routes は上で UPSERT 済み）
        stop_id_map = {r.gtfs_stop_id: r.id for r in conn.execute(text("SELECT id, gtfs_stop_id FROM stops"))}
        route_id_map = {r.gtfs_route_id: r.id for r in conn.execute(text("SELECT id, gtfs_route_id FROM routes"))}

        # 3) route_segments UPSERT ＋ stale削除
        for operator, (_routes_map, segments, _t) in parsed.items():
            seg_rows = []
            for (route_gid, board_gid, alight_gid), ride_min in segments.items():
                rpk = route_id_map.get(route_gid)
                bpk = stop_id_map.get(board_gid)
                apk = stop_id_map.get(alight_gid)
                if rpk is None or bpk is None or apk is None:
                    # 乗降停が F9 投入済み 1,491 停に無い（圏外等）＝スキップ
                    stats["seg_skip_no_stop"] += 1
                    continue
                seg_rows.append({
                    "route_id": rpk, "boarding_stop_id": bpk,
                    "alight_stop_id": apk, "ride_min": ride_min,
                })
            for i in range(0, len(seg_rows), UPSERT_CHUNK):
                _upsert_segments(conn, seg_tbl, seg_rows[i:i + UPSERT_CHUNK])
            stats["seg_upsert"] += len(seg_rows)

            op_route_pks = [pk for gid, pk in route_id_map.items() if gid.startswith(f"{operator}:")]
            seen_keys = [(r["route_id"], r["boarding_stop_id"], r["alight_stop_id"]) for r in seg_rows]
            stats["seg_deleted"] += _delete_stale_segments(conn, seg_tbl, op_route_pks, seen_keys)

            seen_route_gids = {k[0] for k in segments}
            stats["routes_deleted"] += _delete_stale_routes(conn, operator, seen_route_gids)

    return stats


def main(dry_run: bool = False) -> None:
    from batch.db import log_connection_target
    log_connection_target()  # 起動時に接続先（本番/ローカル）を必ず表示＝誤投入防止
    sources = gtfs.load_sources()
    token = gtfs.get_token()

    # 1) 取得（ネットワーク）。1社の失敗はスキップして継続（f9 と同じ堅牢化）
    print("== GTFS 取得（routes/trips/stop_times/calendar）==")
    zips: dict[str, Path] = {}
    failed: dict[str, str] = {}
    for op, src in sources.items():
        try:
            zips[op] = gtfs.download(op, src, token)
        except Exception as e:  # noqa: BLE001 取得失敗は握って次の社へ
            failed[op] = str(e).splitlines()[0]
            print(f"  [取得失敗] {op}: {failed[op]}")

    # 2) パース＋区間算出（DB非依存）
    print("== パース＋区間算出 ==")
    print(f"   「昼」時間帯 = {LUNCH_LABEL}")  # 仮決めが埋もれないよう実行時にも表示
    parsed: dict = {}
    for op, zp in zips.items():
        try:
            parsed[op] = process_operator(op, zp)
        except Exception as e:  # noqa: BLE001 パース失敗も1社スキップ
            failed[op] = str(e).splitlines()[0]
            print(f"  [パース失敗] {op}: {failed[op]}")

    if dry_run:
        print("== ドライラン：DB書込なし ==")
        _print_summary(parsed, failed, db_stats=None)
        return

    # 3) DB反映（1トランザクション）
    print("== routes / route_segments 反映 ==")
    db_stats = _apply_to_db(parsed)
    _print_summary(parsed, failed, db_stats=db_stats)
    print("完了")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="F9 routes/route_segments 取込バッチ")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="DB書込なしで取得〜算出〜サマリのみ（動作確認用）",
    )
    args = ap.parse_args()
    main(dry_run=args.dry_run)
