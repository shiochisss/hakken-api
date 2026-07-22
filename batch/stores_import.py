"""stores 取込バッチ（CSVキュレーション → PostgreSQL）。

hakken-curation が出力する16列CSV（GoogleマップURL→AI構造化）を stores へ UPSERT する。
F9 / route_segments と同じ設計思想（引数CSV → 検証 → UPSERT → dry-run → サマリ）。

確定方針（2026-07-22 承認）:
  1) UPSERTキー = place_id（stores で唯一の UNIQUE）。place_id 空欄行はスキップ。
  2) **stale削除はしない**。stores はキュレーションで累積する性質で、1本のCSVは
     「今回分」であり全宇宙ではない。CSVから消えても既存storeは自動削除しない（意図どおり）。
     更新時は業務/人手所有列 status・is_listed を**触らない**（ON CONFLICT の SET から除外）。
  3) updated_by は **INSERT時のみ 'import' を設定**。UPDATE時は SET に含めない
     （ops が後から手動修正した履歴を、CSV再取込で上書きしないため）。
  4) needs_check 行も投入対象に含める（値はDBの needs_check 列に保存。目視は取込外の人手運用）。
  5) CSVパスは引数指定（python -m batch.stores_import <csv_path> [--dry-run]）。

DB側で自動付与（CSVに無い列。手順書E:124）:
  id（IDENTITY採番）／is_listed（DEFAULT true）／updated_at（DEFAULT now()／更新時 now()）／
  updated_by（DEFAULT無し → INSERT時にバッチが 'import' を供給）。

実行:
  python -m batch.stores_import path/to/stores_YYYYMMDD.csv --dry-run   # DB書込なし
  python -m batch.stores_import path/to/stores_YYYYMMDD.csv             # 本反映
必要な環境変数: DATABASE_URL（本反映時のみ）。dry-run はDB非接続。

方針の根拠:
  - schema_postgres.sql stores（19列＝CSV16＋id/is_listed/updated_at/updated_by）
  - 手順書E 6章（DB取込への受け渡し）／hakken-curation/CLAUDE.md（16列スキーマ・空欄運用）
  - DB設計書 4-1（status＝運営手動UPDATEの唯一の業務列／is_listed＝人のみ操作）
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import date
from pathlib import Path

UPSERT_CHUNK = 500

# CSV 16列（この順・hakken-curation/CLAUDE.md）
CSV_COLS = [
    "name", "category_l", "category_s", "address", "lat", "lng", "place_id",
    "gmaps_url", "hotpepper_url", "insta_url", "official_url", "area_label",
    "status", "confidence", "needs_check", "curated_date",
]

# 必須列（category_s・place_id・各URL・needs_check は任意）
REQUIRED = ("name", "category_l", "address", "lat", "lng", "gmaps_url",
            "area_label", "status", "confidence", "curated_date")

STATUS_ENUM = {"営業中", "一時休業疑い", "閉店疑い", "移転疑い"}
CONFIDENCE_ENUM = {"高", "中", "低"}

# 列長ガード（schema_postgres.sql の VARCHAR(N)。URL系は TEXT＝無制限）
MAXLEN = {
    "name": 255, "category_l": 64, "category_s": 64, "address": 255,
    "place_id": 255, "area_label": 64, "status": 16, "confidence": 2,
}

# 更新時に上書きしてよい列（status・is_listed・updated_by・place_id・id は除外）
UPDATE_COLS = (
    "name", "category_l", "category_s", "address", "lat", "lng", "gmaps_url",
    "hotpepper_url", "insta_url", "official_url", "area_label", "confidence",
    "needs_check", "curated_date",
)

_TALLY_KEYS = (
    "total",
    "ok",
    "skip_required",     # 必須列が空
    "skip_latlng",       # lat/lng が float でない
    "skip_status",       # status が enum 外
    "skip_confidence",   # confidence が enum 外
    "skip_date",         # curated_date が日付でない
    "skip_len",          # 列長超過
    "skip_no_place_id",  # place_id 空（UPSERTキー欠）
    "needs_check",       # 採用行のうち needs_check 記載あり
)


# ============================================================
# 純関数（DB非依存・テスト対象）
# ============================================================

def read_csv_rows(csv_path: Path) -> tuple[list[str], list[dict]]:
    """CSV（UTF-8 BOM許容）を (header, rows) で返す。"""
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        rows = list(reader)
    return header, rows


def validate_row(row: dict) -> tuple[dict | None, str]:
    """CSV1行を検証して UPSERT 用 dict にする。

    返り値: (clean|None, status)
      status: ok / skip_required / skip_latlng / skip_status /
              skip_confidence / skip_date / skip_len / skip_no_place_id
    """
    def g(k: str) -> str:
        return (row.get(k) or "").strip()

    for c in REQUIRED:
        if not g(c):
            return None, "skip_required"
    try:
        lat = float(g("lat"))
        lng = float(g("lng"))
    except ValueError:
        return None, "skip_latlng"
    if g("status") not in STATUS_ENUM:
        return None, "skip_status"
    if g("confidence") not in CONFIDENCE_ENUM:
        return None, "skip_confidence"
    try:
        cdate = date.fromisoformat(g("curated_date"))
    except ValueError:
        return None, "skip_date"
    for col, n in MAXLEN.items():
        if len(g(col)) > n:
            return None, "skip_len"

    pid = g("place_id")
    if not pid:  # UPSERTキーが無い行はスキップ（方針#1）
        return None, "skip_no_place_id"

    clean = {
        "name": g("name"),
        "category_l": g("category_l"),
        "category_s": g("category_s") or None,
        "address": g("address"),
        "lat": lat,
        "lng": lng,
        "place_id": pid,
        "gmaps_url": g("gmaps_url"),
        "hotpepper_url": g("hotpepper_url") or None,
        "insta_url": g("insta_url") or None,
        "official_url": g("official_url") or None,
        "area_label": g("area_label"),
        "status": g("status"),            # INSERT時のみ使用（更新では触らない）
        "confidence": g("confidence"),
        "needs_check": g("needs_check") or None,
        "curated_date": cdate,
        "updated_by": "import",           # INSERT時のみ使用（更新では触らない・方針#3）
    }
    return clean, "ok"


def validate_all(rows: list[dict]) -> tuple[list[dict], dict, Counter, Counter]:
    """全行を検証して (採用行, tally, confidence分布, status分布) を返す。"""
    tally = {k: 0 for k in _TALLY_KEYS}
    clean_rows: list[dict] = []
    conf_dist: Counter = Counter()
    status_dist: Counter = Counter()
    for row in rows:
        tally["total"] += 1
        clean, status = validate_row(row)
        if clean is None:
            tally[status] += 1
            continue
        tally["ok"] += 1
        if clean["needs_check"]:
            tally["needs_check"] += 1
        conf_dist[clean["confidence"]] += 1
        status_dist[clean["status"]] += 1
        clean_rows.append(clean)
    return clean_rows, tally, conf_dist, status_dist


# ============================================================
# サマリ出力
# ============================================================

def _print_summary(csv_path: Path, tally: dict, conf_dist: Counter,
                   status_dist: Counter, db_stats: dict | None) -> None:
    print("== サマリ（stores_import）==")
    print(f"CSV: {csv_path}   読込 {tally['total']} 行")
    print(f"採用可      : {tally['ok']}   （必須充足・enum適合・place_id有）")
    skip_total = (
        tally["skip_no_place_id"] + tally["skip_required"] + tally["skip_status"]
        + tally["skip_confidence"] + tally["skip_latlng"] + tally["skip_date"] + tally["skip_len"]
    )
    print(
        f"スキップ    : {skip_total}   （place_id空 {tally['skip_no_place_id']} / "
        f"必須欠 {tally['skip_required']} / status不正 {tally['skip_status']} / "
        f"confidence不正 {tally['skip_confidence']} / lat-lng不正 {tally['skip_latlng']} / "
        f"日付不正 {tally['skip_date']} / 列長超 {tally['skip_len']}）"
    )
    print(f"needs_check : {tally['needs_check']} 行（採用可のうち。投入対象＝取込前に人が目視確認する運用メモ）")
    print("confidence  : " + " ".join(f"{k}{v}" for k, v in sorted(conf_dist.items())) or "-")
    print("status      : " + " ".join(f"{k}{v}" for k, v in sorted(status_dist.items())) or "-")
    if db_stats is None:
        print("DB反映: （ドライラン＝未反映。place_idキーでの新規/更新の内訳は本反映時に表示）")
    else:
        print(
            f"DB反映: 新規INSERT={db_stats['insert']} / 更新UPDATE={db_stats['update']}"
            f"（更新時 status・is_listed・updated_by は保護＝非更新）"
        )


# ============================================================
# DB接続を伴う部分（Azure Database for PostgreSQL）。--dry-run では呼ばれない。
# ============================================================

def _stores_table(md):
    from sqlalchemy import (BigInteger, Boolean, Column, Date, DateTime,
                            Float, String, Table, Text)
    return Table(
        "stores", md,
        Column("id", BigInteger, primary_key=True),
        Column("name", String),
        Column("category_l", String),
        Column("category_s", String),
        Column("address", String),
        Column("lat", Float),
        Column("lng", Float),
        Column("place_id", String),
        Column("gmaps_url", Text),
        Column("hotpepper_url", Text),
        Column("insta_url", Text),
        Column("official_url", Text),
        Column("area_label", String),
        Column("status", String),
        Column("confidence", String),
        Column("needs_check", Text),
        Column("curated_date", Date),
        Column("is_listed", Boolean),
        Column("updated_at", DateTime(timezone=True)),
        Column("updated_by", String),
    )


def _apply_to_db(clean_rows: list[dict]) -> dict:
    """place_id をキーに UPSERT。更新は UPDATE_COLS + updated_at のみ（方針#2/#3）。"""
    from sqlalchemy import MetaData, func, text
    from sqlalchemy.dialects.postgresql import insert
    from batch.db import get_engine

    stats = {"insert": 0, "update": 0}
    if not clean_rows:
        return stats

    md = MetaData()
    stores = _stores_table(md)
    engine = get_engine()
    with engine.begin() as conn:
        # 新規/更新の内訳（読み取り）：既存 place_id との突合
        existing = {r[0] for r in conn.execute(text("SELECT place_id FROM stores WHERE place_id IS NOT NULL"))}
        pids = [r["place_id"] for r in clean_rows]
        stats["update"] = sum(1 for p in pids if p in existing)
        stats["insert"] = len(pids) - stats["update"]

        for i in range(0, len(clean_rows), UPSERT_CHUNK):
            stmt = insert(stores).values(clean_rows[i:i + UPSERT_CHUNK])
            set_ = {c: getattr(stmt.excluded, c) for c in UPDATE_COLS}
            set_["updated_at"] = func.now()   # 更新の監査時刻のみ触る
            # status / is_listed / updated_by は SET に含めない＝既存値を保護
            stmt = stmt.on_conflict_do_update(index_elements=[stores.c.place_id], set_=set_)
            conn.execute(stmt)
    return stats


def main(csv_path: str, dry_run: bool = False) -> None:
    path = Path(csv_path)
    if not path.exists():
        raise SystemExit(f"CSV が見つかりません: {path}")

    header, rows = read_csv_rows(path)
    missing = [c for c in CSV_COLS if c not in header]
    if missing:
        raise SystemExit(f"CSV に必須列が不足: {missing}（想定16列: {CSV_COLS}）")

    clean_rows, tally, conf_dist, status_dist = validate_all(rows)

    if dry_run:
        print("== ドライラン：DB書込なし ==")
        _print_summary(path, tally, conf_dist, status_dist, db_stats=None)
        return

    print("== stores 反映（UPSERT・stale削除なし）==")
    db_stats = _apply_to_db(clean_rows)
    _print_summary(path, tally, conf_dist, status_dist, db_stats=db_stats)
    print("完了")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="stores 取込バッチ（CSV→PostgreSQL・UPSERT）")
    ap.add_argument("csv_path", help="取込む16列CSVのパス（hakken-curation/output/stores_YYYYMMDD.csv）")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="DB書込なしで検証〜サマリのみ（動作確認用）",
    )
    args = ap.parse_args()
    main(args.csv_path, dry_run=args.dry_run)
