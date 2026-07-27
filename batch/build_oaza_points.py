"""起点住所ラベル用の町丁目代表点データを生成する（オフライン逆ジオコーディングの元データ）。

S2 の「〈住所〉から探しています」に使う住所は、**外部APIを呼ばず自前データの最寄り探索**で出す。
そのためのデータ（`config/oaza_points.json`）を、国土交通省「大字・町丁目位置参照情報」から
1回だけ生成する **ビルド用スクリプト**。夜間バッチではない（データは年次更新なので手動実行）。

    python -m batch.build_oaza_points --dry-run   # 取得〜集計のみ（ファイル書込なし）
    python -m batch.build_oaza_points             # config/oaza_points.json を生成

出典・ライセンス:
  「大字・町丁目位置参照情報 国土交通省」（https://nlftp.mlit.go.jp/isj/）
  利用約款により商用利用・複製・編集は可能だが **出典明示が必須**。加工（矩形での抽出と
  列の間引き）を行っているため、その旨も併せて記載する（画面設計書 B-S5・README）。
  ※公共測量・証明書作成等の高精度用途には使えない（約款の禁止事項）。本用途は
    「起点の目安を人に見せる」だけなので該当しない。

なぜ外部API（国土地理院の逆ジオコーディング）を使わないか:
  実行時に SLA の無い外部APIへ毎検索ぶら下がることを避けるため。オフラインなら
  キャッシュ・タイムアウト・障害時フォールバックの設計が丸ごと不要になる。
  代償は精度で、町丁目の**代表点**への最寄り判定なので境界付近では隣の町丁目名が出うる
  （起点を小数3桁＝約110m格子に丸めている粒度と釣り合う誤差として許容・2026-07-27 判断）。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # hakken-api/
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data" / "isj"
OUT_PATH = CONFIG_DIR / "oaza_points.json"

# 位置参照情報の版。2026-07-27 時点で `19.0b` が最新（20.0b 以降は未公開＝404 を実測確認）。
# 更新時はここだけ変える（zip 名も同じ文字列を含む）。
ISJ_VERSION = "19.0b"
ISJ_URL = "https://nlftp.mlit.go.jp/isj/dls/data/{ver}/{pref}000-{ver}.zip"

# 取得する都道府県。area_bbox.json の矩形（lat 35.53-35.82 / lng 139.56-139.92）が
# 東京都のほかに埼玉（和光・戸田）・千葉（市川・浦安）・神奈川（川崎）にかかるため4都県。
PREF_CODES = ["11", "12", "13", "14"]  # 埼玉・千葉・東京・神奈川

# 矩形の外側にも余白を持たせる。矩形は「バス停を採用する範囲」であって「アプリを使う位置の
# 範囲」ではないため、縁にいるユーザーの最寄り代表点が矩形の外にあることがある。
# 0.05度 ≒ 5.5km（緯度方向）。データが軽い（1件約60バイト）ので余白は広めに取る。
BBOX_MARGIN_DEG = 0.05

# 位置参照情報 大字・町丁目レベル CSV の列名（CP932）。版が変わって列名が変わったら気付ける
# ように、名前で引いて欠けていたらエラーにする（黙って空データを吐かない）。
COL_PREF = "都道府県名"
COL_CITY = "市区町村名"
COL_OAZA = "大字町丁目名"
COL_LAT = "緯度"
COL_LNG = "経度"


def load_bbox() -> dict:
    with open(CONFIG_DIR / "area_bbox.json", encoding="utf-8") as f:
        b = json.load(f)
    return {
        "lat_min": b["lat_min"] - BBOX_MARGIN_DEG,
        "lat_max": b["lat_max"] + BBOX_MARGIN_DEG,
        "lng_min": b["lng_min"] - BBOX_MARGIN_DEG,
        "lng_max": b["lng_max"] + BBOX_MARGIN_DEG,
    }


def download(pref: str) -> Path:
    """都道府県コードの zip を data/isj/ に取得して返す（既にあれば再利用）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    dest = DATA_DIR / f"{pref}000-{ISJ_VERSION}.zip"
    if dest.exists():
        print(f"  既存を再利用: {dest.name}")
        return dest
    url = ISJ_URL.format(ver=ISJ_VERSION, pref=pref)
    print(f"  取得: {pref} <- {url}")
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{pref}: 位置参照情報の取得に失敗 HTTP {e.code}: {url}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"{pref}: 位置参照情報の取得に失敗（ネットワーク）: {e.reason}") from e
    dest.write_bytes(data)
    return dest


def read_rows(zip_path: Path):
    """zip 内の CSV を1行=dict で返す。位置参照情報は CP932（BOM なし）。"""
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise RuntimeError(f"CSV が zip 内に見つかりません: {zip_path}")
        with z.open(names[0]) as raw:
            text = raw.read().decode("cp932")
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in (COL_PREF, COL_CITY, COL_OAZA, COL_LAT, COL_LNG)
               if c not in (reader.fieldnames or [])]
    if missing:
        raise RuntimeError(
            f"{zip_path.name}: 想定した列がありません（版の変更？）: {missing} / "
            f"実際={reader.fieldnames}"
        )
    yield from reader


def build(dry_run: bool) -> None:
    bbox = load_bbox()
    print(f"位置参照情報 {ISJ_VERSION} / 都道府県 {','.join(PREF_CODES)}")
    print(f"抽出矩形（余白 {BBOX_MARGIN_DEG}度込み）: "
          f"lat {bbox['lat_min']:.3f}-{bbox['lat_max']:.3f} / "
          f"lng {bbox['lng_min']:.3f}-{bbox['lng_max']:.3f}")

    points: list[list] = []
    seen: set[tuple] = set()
    total_read = 0
    for pref in PREF_CODES:
        rows = list(read_rows(download(pref)))
        total_read += len(rows)
        kept = 0
        for r in rows:
            try:
                lat = float(r[COL_LAT])
                lng = float(r[COL_LNG])
            except (TypeError, ValueError):
                continue  # 座標欠損行は捨てる（代表点が無い行が稀にある）
            if not (bbox["lat_min"] <= lat <= bbox["lat_max"]):
                continue
            if not (bbox["lng_min"] <= lng <= bbox["lng_max"]):
                continue
            label = f"{r[COL_PREF]}{r[COL_CITY]}{r[COL_OAZA]}"
            # 同一ラベル・同一座標の重複を落とす（丁目の分割で稀に同座標の行がある）
            key = (round(lat, 6), round(lng, 6), label)
            if key in seen:
                continue
            seen.add(key)
            # 座標は小数6桁で足りる（約0.1m）。JSON を小さく保つため丸めて書く
            points.append([round(lat, 6), round(lng, 6), label])
            kept += 1
        print(f"  {pref}: {len(rows):,}行 → 採用 {kept:,}")

    points.sort(key=lambda p: (p[0], p[1]))
    print(f"合計: 読込 {total_read:,}行 → 採用 {len(points):,}点")
    if points:
        print(f"  例: {points[len(points) // 2][2]}")

    if dry_run:
        print("--dry-run: ファイルは書き込まない")
        return
    payload = {
        "_comment": (
            "S2 の起点住所ラベル用の町丁目代表点。batch/build_oaza_points.py で生成（手動・"
            f"年次更新）。出典: 大字・町丁目位置参照情報 国土交通省（{ISJ_VERSION}）を"
            "エリア矩形で抽出・列を間引いて加工。points の各要素は [緯度, 経度, 住所]。"
        ),
        "isj_version": ISJ_VERSION,
        "pref_codes": PREF_CODES,
        "bbox_margin_deg": BBOX_MARGIN_DEG,
        "points": points,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    size_kb = OUT_PATH.stat().st_size / 1024
    print(f"書込: {OUT_PATH.relative_to(BASE_DIR)}  {len(points):,}点 / {size_kb:,.0f} KB")


def main() -> None:
    ap = argparse.ArgumentParser(description="町丁目代表点データ（config/oaza_points.json）を生成")
    ap.add_argument("--dry-run", action="store_true", help="取得・集計のみ（書込なし）")
    build(ap.parse_args().dry_run)


if __name__ == "__main__":
    main()
