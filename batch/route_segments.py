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
       - stop_times.txt を trip 単位・stop_sequence 順に並べ、**同一便の下流全停ペア**を
         区間化（2026-07-26 変更・DB設計書 9章#16 対応）。
         ※旧実装は隣接停ペアのみ（#7-c）だった。reach は区間を最大2つしか繋がないため
           「バスに乗れるのは最大2停」という制約になり、「徒歩20分＋バス1分」のような
           経路が最短として返っていた。ペア生成は f9_stops と同一の矩形フィルタで
           **圏内の停に限定**する（圏外停は通過扱い＝所要時間には含まれる）。
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
import json
import statistics
from collections import defaultdict
from pathlib import Path

from batch import f9_stops, gtfs_reader as gtfs

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
# 【既知の制限】土日判定は calendar.txt 方式。calendar_dates.txt のみの事業者は
#   土日集合が空になり route_segments が0件になる。
#   → 該当した小田急（odakyu）は **2026-07-26 に取得対象から外した**。
#     bbox を東京23区へ拡大した際、停は 8→793 に増えたが区間は0のままで
#     「停はあるがバスが来ない」状態が広がるだけだったため（経緯は
#     config/gtfs_sources.json の "_removed_2026-07-26" に記録）。
#   復活させる場合は weekend_service_ids に calendar_dates.txt 方式の分岐を追加する。
WEEKEND_COLS = ("saturday", "sunday")

# ★★★ 「本数少なめ」のしきい値は暫定で 2本未満（2026-07-26 ibes 判断）。★★★
#   母数は「土日の LUNCH_START_SEC〜LUNCH_END_SEC に乗車停を出発する便」＝ trip_count。
#   下流全停ペア化（9章#16）の副作用で、長い区間は通しで走る便しか該当せず、
#   店へ到達できる区間に絞ると 1本の割合が 12.5%（全区間では 3.0%）に上がる。
#   除外はせず「🚌 本数少なめ」バッジで開示する方針（引き継ぎ資料4章）。
#   判定を行うのは API 側（app/routers/search.py）だが、定数の定義はここ1箇所に置く。
FEW_TRIPS_THRESHOLD = 2

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
    "skip_not_lunch",    # 乗車停出発が昼の時間帯外（乗車停単位）
    "skip_time_bad",     # 時刻パース不可の停
    "skip_nonpos",       # 所要が負値・0秒のペア
    "skip_no_stop_id",   # stop_id 欠損の停
    "skip_out_of_scope",  # stops（F9投入済み）に無い停＝圏外。ペア化の対象外
    "skip_zero_min",     # 中央値が分に丸めると0分になった区間
    "stops_in_trip",     # 圏内としてペア化に使った停の総数（便ごとの合計）
    "pair_kept",         # 採用したペア（集約前）
    "segments",          # 集約後の区間数（route,乗車,降車 のユニーク）
)


def load_bbox() -> dict | None:
    """f9_stops と同一のエリア矩形設定を読む（DB非依存）。

    stops テーブルへ投入された停と同じ圏内判定にするため、同じ config を使う。
    enabled=false のときは None＝矩形フィルタ無効（全stop採用）。
    """
    with open(gtfs.CONFIG_DIR / "area_bbox.json", encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg if cfg.get("enabled", False) else None


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


def build_segments(
    operator: str,
    st_rows,
    trips: dict,
    weekend: set[str],
    tally: dict,
    in_scope: set[str] | None = None,
) -> dict:
    """同一便の「下流にある全停」ペアの所要秒を集め、中央値で集約して
    {(route_gid, 乗車gid, 降車gid): (ride_min, trip_count)} を返す。

    【2026-07-26 変更・DB設計書 9章#16 対応】
    旧実装は隣接停ペア（`zip(rows, rows[1:])`）のみを区間化していた。reach は区間を
    最大2つしか繋がない（直行=1区間／hub経由=2区間）ため、**バスに乗れるのが最大2停**
    という制約になり、「乗車停まで徒歩20分＋バス1分」のような経路が最短として返っていた。
    本実装では同一便の下流全停へのペアを作り、ride_min は実時刻の差（通過停ぶんを含む）
    で算出する。これにより「近くの停から数停乗る」経路が選択肢に入る。

    - gtfs_stop_id / gtfs_route_id は f9_stops と同じ "operator:原ID" 規則で付与し一貫させる。
    - in_scope（f9_stops.parse_zip が返す採用停の集合）を渡すと、**圏内の停だけ**でペアを作る。
      圏外の停は「通過」として扱われ、所要時間には含まれる（実時刻の差で計算するため）。
      これを渡さないと全国の停で O(n^2) になり破綻するので、本番経路では必ず渡す。
    - 昼フィルタは乗車停の出発時刻（無ければ到着）で判定。
    - ride_min = 降車停の到着 − 乗車停の出発（秒）→ 中央値 → 分へ丸め。
    - trip_count = 中央値の母数＝その区間を土日昼に走る便数（acc[key] の長さ）。
      「本数少なめ」バッジ（FEW_TRIPS_THRESHOLD）の判定に使う。
    - 環状路線で同一停が再登場する便では、同一停どうしのペアは作らない。
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

        # 便を「圏内の停だけ」の並びに落とす（出発秒・到着秒つき）
        seq: list[tuple[str, int, int]] = []
        for r in rows:
            raw = (r.get("stop_id") or "").strip()
            if not raw:
                tally["skip_no_stop_id"] += 1
                continue
            gid = f"{operator}:{raw}"
            if in_scope is not None and gid not in in_scope:
                tally["skip_out_of_scope"] += 1
                continue
            dep = parse_gtfs_time(r.get("departure_time") or r.get("arrival_time"))
            arr = parse_gtfs_time(r.get("arrival_time") or r.get("departure_time"))
            if dep is None or arr is None:
                tally["skip_time_bad"] += 1
                continue
            seq.append((gid, dep, arr))
        tally["stops_in_trip"] += len(seq)

        # 下流の全停ペア（i < j）。ride は実時刻の差なので通過停ぶんも含む。
        for i, (gid_a, dep_a, _arr_a) in enumerate(seq):
            if not in_lunch_window(dep_a):
                tally["skip_not_lunch"] += 1
                continue
            route_gid = f"{operator}:{rid}"
            for gid_b, _dep_b, arr_b in seq[i + 1:]:
                if gid_b == gid_a:  # 環状路線で同一停が再登場
                    continue
                ride = arr_b - dep_a
                if ride <= 0:  # 負値・0秒は除外（#7-e）
                    tally["skip_nonpos"] += 1
                    continue
                acc[(route_gid, gid_a, gid_b)].append(ride)
                tally["pair_kept"] += 1

    segments: dict[tuple[str, str, str], tuple[int, int]] = {}
    for key, rides in acc.items():
        ride_min = int(round(statistics.median(rides) / 60))
        if ride_min <= 0:  # 中央値が分に丸めて0分（超短区間）はスキップ
            tally["skip_zero_min"] += 1
            continue
        segments[key] = (ride_min, len(rides))  # len(rides) = 土日昼の便数
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

    # 圏内の停だけでペアを作るため、f9_stops と同一の矩形フィルタで採用停を求める（DB非依存）。
    # これを渡さないと下流全停ペアが全国規模の O(n^2) になり破綻する（build_segments の docstring 参照）。
    _rows, in_scope, _st = f9_stops.parse_zip(operator, zip_path, load_bbox())

    segments = build_segments(
        operator, gtfs.read_table(zip_path, "stop_times.txt"), trips, weekend, tally, in_scope
    )
    return routes_map, segments, tally


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n]


# ============================================================
# サマリ出力
# ============================================================

def _print_summary(parsed: dict, failed: dict | None, db_stats: dict | None) -> None:
    print("== サマリ（route_segments）==")
    print(f"「昼」時間帯フィルタ = {LUNCH_LABEL}")  # #7-b が仮決めであることを毎回明示
    print("区間の作り方 = 同一便の下流全停ペア（DB設計書 9章#16 対応・2026-07-26）")
    header = (
        f"{'社':<10}{'土日SVC':>7}{'便数':>7}{'土日便':>7}{'圏内停':>8}"
        f"{'採用対':>9}{'区間':>8}{'非土日':>8}{'圏外停':>9}{'昼外':>7}{'非正':>7}"
    )
    print(header)
    total_seg = 0
    for operator, (_routes, _segs, t) in parsed.items():
        total_seg += t["segments"]
        print(
            f"{operator:<10}{t['weekend_services']:>7}{t['trips_total']:>7}{t['trip_weekend']:>7}"
            f"{t['stops_in_trip']:>8}{t['pair_kept']:>9}{t['segments']:>8}"
            f"{t['skip_not_weekend']:>8}{t['skip_out_of_scope']:>9}"
            f"{t['skip_not_lunch']:>7}{t['skip_nonpos']:>7}"
        )
    print(f"{'合計区間':<10}{total_seg:>21}")

    # 便数分布。しきい値（FEW_TRIPS_THRESHOLD）が妥当かを毎回目視できるようにする。
    # ※ここは「全区間」の分布。店へ到達できる区間に絞ると1本の割合はもっと高い（引き継ぎ資料4章）。
    counts = [tc for _r, segs, _t in parsed.values() for _rm, tc in segs.values()]
    if counts:
        few = sum(1 for c in counts if c < FEW_TRIPS_THRESHOLD)
        print(
            f"便数分布（土日{LUNCH_LABEL.split(' ')[0]}）: "
            f"1本 {sum(1 for c in counts if c == 1)} / "
            f"2-3本 {sum(1 for c in counts if 2 <= c <= 3)} / "
            f"4本以上 {sum(1 for c in counts if c >= 4)} / "
            f"中央値 {int(statistics.median(counts))}"
        )
        print(f"  うち「本数少なめ」(<{FEW_TRIPS_THRESHOLD}本): {few} 区間（{few / len(counts) * 100:.1f}%）")

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
        Column("trip_count", Integer),
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
        set_={"ride_min": stmt.excluded.ride_min, "trip_count": stmt.excluded.trip_count},
    )
    conn.execute(stmt)


def _delete_segments_of_routes(conn, seg_tbl, op_route_pks: list[int]) -> int:
    """この社の路線に属する route_segments を全削除する（INSERT の直前に呼ぶ＝全量再生成）。

    【2026-07-26 変更】旧実装は「今回生成しなかった複合キーだけを削除」する方式で、
    `tuple_(...).notin_(seen_keys)` に生成済みキーを全件バインドしていた。下流全停ペアへの
    拡張（9章#16）で区間が1万件超になり、**PostgreSQL のバインドパラメータ上限**
    （65,535）を超えて失敗した。該当社の区間を消してから入れ直す方式に変更する。
    区間は毎回全量生成されるため、結果は従来と同じ（同一トランザクション内で完結）。
    """
    from sqlalchemy import delete
    if not op_route_pks:
        return 0
    stmt = delete(seg_tbl).where(seg_tbl.c.route_id.in_(op_route_pks))
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
            for (route_gid, board_gid, alight_gid), (ride_min, trip_count) in segments.items():
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
                    "trip_count": trip_count,
                })
            # 全量再生成：この社の区間を削除してから INSERT する（削除→投入の順序が必須）
            op_route_pks = [pk for gid, pk in route_id_map.items() if gid.startswith(f"{operator}:")]
            stats["seg_deleted"] += _delete_segments_of_routes(conn, seg_tbl, op_route_pks)

            for i in range(0, len(seg_rows), UPSERT_CHUNK):
                _upsert_segments(conn, seg_tbl, seg_rows[i:i + UPSERT_CHUNK])
            stats["seg_upsert"] += len(seg_rows)

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
