"""F9: GTFS-JP から stops テーブルを生成する夜間バッチ（stops 取込のみ）。

処理の流れ:
  1) 各社の GTFS zip を取得（ネットワーク。1社失敗はスキップして継続）。
     date が要る社（requires_date）は CKAN から最新 date を自動解決（方針A）。
  2) パース：stops.txt を location_type=0 でフィルタ（エリア矩形は area_bbox.json の
     enabled=true のときのみ適用。既定 OFF＝全stop採用）。gtfs_stop_id に "operator:"
     を付与。**列長ガード**で長さエラーを予防（id は切詰不可＝スキップ／name は上限で切詰）
  3) DB反映（1トランザクション）：gtfs_stop_id をキーに UPSERT → 未取込停を削除
     （store_stops/reach から未参照のもののみ）→ is_hub をホワイトリストで再計算
  4) サマリ出力

  ※ --dry-run で 1〜2＋サマリのみ実行（DB書込なし・動作確認用）。
    DB種別=Azure Database for PostgreSQL に確定（2026-07-19）。3) は有効。

方針の根拠（DB設計書 v1.3）:
  - 4-2 stops / 9章#5（洗い替え時の id 安定性）＝ gtfs_stop_id キーの UPSERT で id を維持
  - 事業者跨ぎの stop_id 衝突回避 ＝ "operator:" プレフィックス（DDL変更なし）
  - 統合しない（location_type=0 のみ・GTFS stop 単位で素通し）＝ レビュー合意 2026-07-19

実行: (.venv 有効化後)
  python -m batch.f9_stops --dry-run    # DB書込なしで取得〜パース確認
  python -m batch.f9_stops              # 本反映
必要な環境変数（hakken-api/.env）: DATABASE_URL / ODPT_CONSUMER_KEY /
（任意）{OP}_GTFS_DATE（CKAN自動解決の失敗時フォールバック。例 KANTO_GTFS_DATE）
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from batch import gtfs_reader as gtfs

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
UPSERT_CHUNK = 500

# 列長の上限（DDL schema_postgres.sql 準拠：PostgreSQL 確定）。
# これを超える値をそのまま入れると PostgreSQL では
# "value too long for type character varying(N)"（22001）になるため、DBに触る前（パース層）で吸収する。
MAX_GTFS_STOP_ID = 128
MAX_STOP_NAME = 255

# tally（集計）のキー。skip_* は不採用理由の内訳
_TALLY_KEYS = (
    "total",
    "kept",
    "name_truncated",
    "skip_type",
    "skip_coord",
    "skip_bbox",
    "skip_no_id",
    "skip_id_len",
    "dup",
)


def _load_json(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def build_row(operator: str, row: dict, bbox: dict | None) -> tuple[dict | None, str]:
    """GTFS の1行を検証して UPSERT 用 dict にする（DB非依存・テスト対象）。

    bbox が None のときは矩形フィルタ（skip_bbox）だけ無効化し、他ガード
    （location_type=0・座標パース・ID長）は必ず維持する（首都圏拡大＝全stop採用）。

    返り値: (row|None, status)
      status: ok / name_truncated（採用）
              skip_type / skip_coord / skip_bbox / skip_no_id / skip_id_len（不採用）
    """
    lt = (row.get("location_type") or "").strip()
    if lt not in ("", "0"):  # 駅・出入口などは除外（実停留所のみ）
        return None, "skip_type"
    try:
        lat = float(row["stop_lat"])
        lng = float(row["stop_lon"])
    except (KeyError, ValueError, TypeError):
        return None, "skip_coord"
    if bbox is not None and not (
        bbox["lat_min"] <= lat <= bbox["lat_max"]
        and bbox["lng_min"] <= lng <= bbox["lng_max"]
    ):
        return None, "skip_bbox"

    raw_id = (row.get("stop_id") or "").strip()
    if not raw_id:
        return None, "skip_no_id"
    gid = f"{operator}:{raw_id}"
    if len(gid) > MAX_GTFS_STOP_ID:
        # キーは切り詰めると衝突しうるため、切詰せずスキップ（件数はログに残す）
        return None, "skip_id_len"

    name = (row.get("stop_name") or "").strip()
    status = "ok"
    if len(name) > MAX_STOP_NAME:  # name は表示用途のため上限で切詰
        name = name[:MAX_STOP_NAME]
        status = "name_truncated"

    return {"gtfs_stop_id": gid, "name": name, "lat": lat, "lng": lng, "is_hub": False}, status


def parse_zip(operator: str, zip_path: Path, bbox: dict | None) -> tuple[list[dict], set[str], dict]:
    """zip をパースして (採用行, 採用id集合, tally) を返す（DB非依存・テスト対象）。
    bbox=None で矩形フィルタ無効（全stop採用）。"""
    rows: list[dict] = []
    seen: set[str] = set()
    tally = {k: 0 for k in _TALLY_KEYS}
    for raw in gtfs.read_stops(zip_path):
        tally["total"] += 1
        built, status = build_row(operator, raw, bbox)
        if built is None:
            tally[status] += 1
            continue
        if built["gtfs_stop_id"] in seen:  # zip内の重複stop_id防御
            tally["dup"] += 1
            continue
        seen.add(built["gtfs_stop_id"])
        rows.append(built)
        tally["kept"] += 1
        if status == "name_truncated":
            tally["name_truncated"] += 1
    return rows, seen, tally


def _print_summary(parsed: dict, hub_count: int | None, failed: dict | None = None) -> None:
    print("== サマリ ==")
    header = f"{'社':<10}{'読込':>7}{'採用':>7}{'名切詰':>7}{'型外':>6}{'座標無':>7}{'圏外':>7}{'ID無':>6}{'ID長':>6}{'重複':>6}"
    print(header)
    total_kept = 0
    for operator, (_rows, _seen, t) in parsed.items():
        total_kept += t["kept"]
        print(
            f"{operator:<10}{t['total']:>7}{t['kept']:>7}{t['name_truncated']:>7}"
            f"{t['skip_type']:>6}{t['skip_coord']:>7}{t['skip_bbox']:>7}"
            f"{t['skip_no_id']:>6}{t['skip_id_len']:>6}{t['dup']:>6}"
        )
    print(f"{'合計採用':<10}{total_kept:>21}")
    if failed:
        print(f"取得失敗: {len(failed)} 社 -> " + ", ".join(f"{op}({msg})" for op, msg in failed.items()))
    else:
        print("取得失敗: 0 社")
    if hub_count is None:
        print("is_hub 付与: （ドライラン＝DB未反映）")
    else:
        print(f"is_hub 付与: {hub_count} 停")


# ============================================================
# 以下は DB 接続を伴う部分。DB種別=Azure Database for PostgreSQL に確定（2026-07-19）。
#   PostgreSQL 方言（ON CONFLICT / expanding NOT IN）。--dry-run では呼ばれない。
# ============================================================

def _upsert(conn, rows: list[dict]) -> None:
    from sqlalchemy import Boolean, Column, Float, MetaData, String, Table
    from sqlalchemy.dialects.postgresql import insert

    md = MetaData()
    stops_table = Table(
        "stops", md,
        Column("gtfs_stop_id", String, primary_key=True),
        Column("name", String),
        Column("lat", Float),
        Column("lng", Float),
        Column("is_hub", Boolean),
    )
    stmt = insert(stops_table).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[stops_table.c.gtfs_stop_id],
        set_={"name": stmt.excluded.name, "lat": stmt.excluded.lat, "lng": stmt.excluded.lng},
    )  # is_hub は触らない（後段で再計算）
    conn.execute(stmt)


def _delete_stale(conn, operator: str, seen: set[str]) -> int:
    from sqlalchemy import bindparam, text

    if not seen:
        print(f"  [警告] {operator}: 取込0件のため未取込停の削除はスキップ")
        return 0
    stmt = text(
        """
        DELETE FROM stops s
        WHERE s.gtfs_stop_id LIKE :prefix
          AND s.gtfs_stop_id NOT IN :seen
          AND NOT EXISTS (SELECT 1 FROM store_stops ss WHERE ss.stop_id = s.id)
          AND NOT EXISTS (
                SELECT 1 FROM reach r
                WHERE r.boarding_stop_id = s.id
                   OR r.alight_stop_id = s.id
                   OR r.via_hub_id = s.id)
        """
    ).bindparams(bindparam("seen", expanding=True))
    res = conn.execute(stmt, {"prefix": f"{operator}:%", "seen": list(seen)})
    return res.rowcount or 0


def _apply_hub(conn, hub_cfg: dict) -> int:
    from sqlalchemy import text

    if hub_cfg.get("match", "name_contains") != "name_contains":
        raise RuntimeError(f"未対応の hub match 方式: {hub_cfg.get('match')}")
    names = hub_cfg.get("names", [])
    conn.execute(text("UPDATE stops SET is_hub = false WHERE is_hub = true"))
    if not names:
        return 0
    clauses = " OR ".join(f"name LIKE :p{i}" for i in range(len(names)))
    params = {f"p{i}": f"%{n}%" for i, n in enumerate(names)}
    res = conn.execute(text(f"UPDATE stops SET is_hub = true WHERE {clauses}"), params)
    return res.rowcount or 0


def main(dry_run: bool = False) -> None:
    sources = gtfs.load_sources()
    bbox_cfg = _load_json("area_bbox.json")
    # enabled=false（既定）なら bbox=None＝矩形フィルタ無効（全stop採用）
    bbox = bbox_cfg if bbox_cfg.get("enabled", False) else None
    hub_cfg = _load_json("hub_stops.json")
    token = gtfs.get_token()

    # 1) 取得（ネットワーク）。1社の失敗はスキップして継続する（堅牢化）
    print(f"== GTFS 取得（bbox={'ON' if bbox else 'OFF'}）==")
    zips: dict[str, Path] = {}
    failed: dict[str, str] = {}
    for op, src in sources.items():
        try:
            zips[op] = gtfs.download(op, src, token)
        except Exception as e:  # noqa: BLE001 取得失敗は握って次の社へ
            failed[op] = str(e).splitlines()[0]
            print(f"  [取得失敗] {op}: {failed[op]}")

    # 2) パース＋列長ガード（DB非依存）
    print("== パース ==")
    parsed = {op: parse_zip(op, zp, bbox) for op, zp in zips.items()}

    if dry_run:
        print("== ドライラン：DB書込なし ==")
        _print_summary(parsed, hub_count=None, failed=failed)
        return

    # 3) DB反映（1トランザクション）
    from batch.db import get_engine

    print("== stops 反映 ==")
    engine = get_engine()
    with engine.begin() as conn:
        for operator, (rows, seen, _t) in parsed.items():
            for i in range(0, len(rows), UPSERT_CHUNK):
                _upsert(conn, rows[i : i + UPSERT_CHUNK])
            _delete_stale(conn, operator, seen)
        hub_count = _apply_hub(conn, hub_cfg)
    _print_summary(parsed, hub_count, failed=failed)
    print("完了")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="F9 stops 取込バッチ")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="DB書込なしで取得〜パース〜サマリのみ（動作確認用）",
    )
    args = ap.parse_args()
    main(dry_run=args.dry_run)
