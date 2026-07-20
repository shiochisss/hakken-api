"""F9 stops バッチのパース層テスト（DB・ネットワーク不要）。

実行: (.venv 有効化後)  python -m tests.test_f9_stops
pytest 未導入のため素の assert で書く（新規依存なし）。
"""
from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path

from batch import f9_stops as f

BBOX = {"lat_min": 35.70, "lat_max": 35.79, "lng_min": 139.58, "lng_max": 139.70}

_COLS = "stop_id,stop_name,stop_lat,stop_lon,location_type"


def _row(sid, name, lat, lng, lt=""):
    return f"{sid},{name},{lat},{lng},{lt}"


def _make_zip(rows):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("gtfs/stops.txt", _COLS + "\n" + "\n".join(rows) + "\n")
    tmp = Path(tempfile.gettempdir()) / "hakken_f9_test.zip"
    tmp.write_bytes(buf.getvalue())
    return tmp


def test_build_row_statuses():
    ok, s = f.build_row("seibu", {"stop_id": "1", "stop_name": "練馬駅前", "stop_lat": "35.7377", "stop_lon": "139.6547", "location_type": "0"}, BBOX)
    assert s == "ok" and ok["gtfs_stop_id"] == "seibu:1"

    # 駅（location_type=1）は除外
    _, s = f.build_row("seibu", {"stop_id": "2", "stop_name": "駅", "stop_lat": "35.74", "stop_lon": "139.65", "location_type": "1"}, BBOX)
    assert s == "skip_type"

    # 座標欠損
    _, s = f.build_row("seibu", {"stop_id": "3", "stop_name": "x", "stop_lat": "", "stop_lon": "", "location_type": "0"}, BBOX)
    assert s == "skip_coord"

    # 矩形外
    _, s = f.build_row("seibu", {"stop_id": "4", "stop_name": "x", "stop_lat": "35.60", "stop_lon": "139.70", "location_type": "0"}, BBOX)
    assert s == "skip_bbox"

    # stop_id 空
    _, s = f.build_row("seibu", {"stop_id": "", "stop_name": "x", "stop_lat": "35.74", "stop_lon": "139.65", "location_type": "0"}, BBOX)
    assert s == "skip_no_id"


def test_length_guard():
    # gtfs_stop_id が上限超（切詰不可＝スキップ）
    long_id = "X" * (f.MAX_GTFS_STOP_ID + 10)
    row, s = f.build_row("seibu", {"stop_id": long_id, "stop_name": "x", "stop_lat": "35.74", "stop_lon": "139.65", "location_type": "0"}, BBOX)
    assert row is None and s == "skip_id_len"

    # ちょうど上限に収まる id は採用（"seibu:" 分を差し引く）
    fit_id = "Y" * (f.MAX_GTFS_STOP_ID - len("seibu:"))
    row, s = f.build_row("seibu", {"stop_id": fit_id, "stop_name": "x", "stop_lat": "35.74", "stop_lon": "139.65", "location_type": "0"}, BBOX)
    assert s == "ok" and len(row["gtfs_stop_id"]) == f.MAX_GTFS_STOP_ID

    # name が上限超 → 切詰して採用
    long_name = "あ" * (f.MAX_STOP_NAME + 50)
    row, s = f.build_row("seibu", {"stop_id": "9", "stop_name": long_name, "stop_lat": "35.74", "stop_lon": "139.65", "location_type": "0"}, BBOX)
    assert s == "name_truncated" and len(row["name"]) == f.MAX_STOP_NAME


def test_parse_zip_tally_and_dedup():
    rows = [
        _row("1", "練馬駅前", "35.7377", "139.6547", "0"),   # 採用
        _row("1", "練馬駅前(重複)", "35.7377", "139.6547", "0"),  # dup
        _row("2", "圏外停", "35.60", "139.70", "0"),          # skip_bbox
        _row("3", "駅舎", "35.74", "139.65", "1"),            # skip_type
        _row("X" * 200, "長ID停", "35.74", "139.65", "0"),    # skip_id_len
    ]
    zp = _make_zip(rows)
    got, seen, tally = f.parse_zip("seibu", zp, BBOX)
    zp.unlink()
    assert tally["total"] == 5
    assert tally["kept"] == 1
    assert tally["dup"] == 1
    assert tally["skip_bbox"] == 1
    assert tally["skip_type"] == 1
    assert tally["skip_id_len"] == 1
    assert seen == {"seibu:1"}
    assert got[0]["gtfs_stop_id"] == "seibu:1"


def test_bbox_off_build_row():
    # bbox=None：矩形外でも採用（skip_bbox は起きない）
    row, s = f.build_row("seibu", {"stop_id": "9", "stop_name": "遠方停", "stop_lat": "35.60", "stop_lon": "139.90", "location_type": "0"}, None)
    assert s == "ok" and row["gtfs_stop_id"] == "seibu:9"
    # bbox=None でも他ガードは維持：location_type=1 は除外
    _, s = f.build_row("seibu", {"stop_id": "10", "stop_name": "駅", "stop_lat": "35.60", "stop_lon": "139.90", "location_type": "1"}, None)
    assert s == "skip_type"
    # bbox=None でも座標欠損は除外
    _, s = f.build_row("seibu", {"stop_id": "11", "stop_name": "x", "stop_lat": "", "stop_lon": "", "location_type": "0"}, None)
    assert s == "skip_coord"


def test_bbox_off_parse_zip():
    rows = [
        _row("1", "圏内停", "35.7377", "139.6547", "0"),  # 矩形内
        _row("2", "圏外停", "35.60", "139.90", "0"),       # 矩形外だが bbox OFF で採用
        _row("3", "駅舎", "35.60", "139.90", "1"),         # location_type=1 は除外
    ]
    zp = _make_zip(rows)
    got, seen, tally = f.parse_zip("seibu", zp, None)  # bbox 無効
    zp.unlink()
    assert tally["total"] == 3
    assert tally["kept"] == 2        # 圏外停も採用
    assert tally["skip_bbox"] == 0   # 矩形フィルタは無効
    assert tally["skip_type"] == 1
    assert seen == {"seibu:1", "seibu:2"}


def main():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok: {t.__name__}")
    print(f"全 {len(tests)} テスト成功")


if __name__ == "__main__":
    main()
