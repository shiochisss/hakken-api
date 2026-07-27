"""検索の起点（現在地）を「住所ラベル」に変換し、セッションに記録する。

## なぜ必要か

実機テストで「**現在地がどこからなのか分からない**ため、提示される楽なルートの信ぴょう性が
薄い」という指摘があった（2026-07-27）。S2 が「現在地から探しています」しか出しておらず、
GPS が数百m ずれていてもユーザーが気付けないため、結果そのものを疑う状態になっていた。
そこで起点を住所で明示し（表示）、あとから「その提案はどこ起点だったか」を辿れるように
記録する（`sessions.origin_*` → `going_list.origin_*`）。

## 逆ジオコーディングを外部APIでやらない理由

住所変換は **外部APIを呼ばず、自前データの最寄り探索**で行う（2026-07-27 判断）。
検討した選択肢:

| 案 | 却下・採用の理由 |
|---|---|
| 国土地理院の逆ジオコーディングAPI | 境界でも正確だが、公式SLAの無いAPIに毎検索ぶら下がる。キャッシュ・タイムアウト・障害時フォールバックの設計が必要になる |
| `jageocoder` | 逆ジオコーディング可だが辞書DBが必須（全国 gaiku で 351MB zip・jukyo は 4.5GB）。GitHubの100MB上限・本番 App Service **Free F1 のディスク1GB** に載らない |
| `reverse_geocoder` | オフラインだが GeoNames の市区町村レベルかつローマ字（"Nerima"）で要件未達。scipy/numpy 依存も重い |
| **町丁目代表点を同梱して最寄り探索（採用）** | 追加ライブラリ0・外部API0・障害点0。データは 6,735点／412KB（`config/oaza_points.json`） |

代償は精度で、町丁目の**代表点**への最寄り判定なので境界付近では隣の町丁目名が出うる。
起点を小数3桁（約110m格子）に丸めている粒度と釣り合う誤差として許容する。

## プライバシー

**位置情報の生値は保存しない**（DB設計書1章-5）。`round_origin` で小数3桁に丸めた値だけを
DBに渡す。DB側も `NUMERIC(6,3)` なので、仮に丸め忘れても型が粒度を保証する（二重の防御）。

出典: 「大字・町丁目位置参照情報 国土交通省」（エリア矩形での抽出・列の間引きを行い加工）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from sqlalchemy import text

from app.geo import haversine_m

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "oaza_points.json"

# 起点座標の丸め桁数。3桁 ≒ 110m 格子（暫定）。DBの NUMERIC(6,3) と必ず揃えること。
ORIGIN_PRECISION = 3

# 代表点をバケツ分けする格子の1辺（度）。0.01度 ≒ 1.1km（緯度方向）。
# 6,735点を全探索すると数msかかるので、格子で候補を数点に絞ってから距離を測る
# （検索の `_search_bbox` と同じ「粗く絞る→正確に測る」の2段構え）。
GRID_DEG = 0.01

# 探索する格子の広がり。まず自分と隣接（3x3＝±1.1km）、見つからなければ 7x7（±3.3km）まで
# 広げて諦める。3.3km 先の町丁目名を「起点」と称するのは無理があるため打ち切る（暫定）。
GRID_RINGS = (1, 3)

# ラベルの上限長。DBの VARCHAR(120) に合わせる（超える住所は実在しないが安全側で切る）。
MAX_LABEL_LEN = 120

_grid: dict[tuple[int, int], list[tuple[float, float, str]]] | None = None


def _cell(lat: float, lng: float) -> tuple[int, int]:
    return (int(lat / GRID_DEG), int(lng / GRID_DEG))


def _load_grid() -> dict[tuple[int, int], list[tuple[float, float, str]]]:
    """`config/oaza_points.json` を読み、格子インデックスを1回だけ組む。

    データが無い・壊れている場合は**空の索引**にして例外を投げない。起点ラベルは
    「あると嬉しい表示」であって検索の成否には関わらないため、ここで落として
    検索APIを 500 にしてはいけない。
    """
    global _grid
    if _grid is not None:
        return _grid
    grid: dict[tuple[int, int], list[tuple[float, float, str]]] = {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            payload = json.load(f)
        for lat, lng, label in payload.get("points", []):
            grid.setdefault(_cell(lat, lng), []).append((float(lat), float(lng), str(label)))
    except (OSError, ValueError, TypeError):
        grid = {}
    _grid = grid
    return _grid


def round_origin(lat: float, lng: float) -> tuple[float, float]:
    """起点座標を保存できる粒度（小数3桁 ≒ 110m）に丸める。生値をDBに渡さない唯一の入口。"""
    return (round(lat, ORIGIN_PRECISION), round(lng, ORIGIN_PRECISION))


def _candidates(lat: float, lng: float, ring: int) -> Iterable[tuple[float, float, str]]:
    grid = _load_grid()
    ci, cj = _cell(lat, lng)
    for i in range(ci - ring, ci + ring + 1):
        for j in range(cj - ring, cj + ring + 1):
            yield from grid.get((i, j), ())


def nearest_oaza_label(lat: float, lng: float) -> str | None:
    """現在地に最も近い町丁目代表点の住所ラベル。近くに1点も無ければ None。"""
    for ring in GRID_RINGS:
        best: tuple[float, str] | None = None
        for plat, plng, label in _candidates(lat, lng, ring):
            d = haversine_m(lat, lng, plat, plng)
            if best is None or d < best[0]:
                best = (d, label)
        if best is not None:
            return best[1][:MAX_LABEL_LEN]
    return None


def resolve_origin(lat: float, lng: float, fallback_stop: str | None = None) -> dict:
    """起点の表示用情報を作る。

    返り値（`GET /api/search` の `meta.origin` にそのまま載る形）:
      - `label`        … 住所ラベル。解決できなければ None
      - `nearest_stop` … 最寄りバス停名（呼び出し側が渡す。無ければ None）
      - `source`       … `"oaza"`（住所が出た）／`"stop"`（住所は出ず停名で代替）／`"none"`

    フロントは `source` で表示を3分岐する（画面設計書 B-S2）。ここで例外は出さない。
    """
    label = nearest_oaza_label(lat, lng)
    if label:
        return {"label": label, "nearest_stop": fallback_stop, "source": "oaza"}
    if fallback_stop:
        return {"label": None, "nearest_stop": fallback_stop, "source": "stop"}
    return {"label": None, "nearest_stop": None, "source": "none"}


def save_session_origin(conn, sid: int, lat: float, lng: float, label: str | None) -> None:
    """セッションに直近の検索起点を記録する（丸めた座標のみ・生値は渡さない）。

    `GET` が書き込むのは API設計書 A-8「GET は冪等」の例外だが、同じ依存
    （`get_current_session`）が既に `last_seen_at` を UPDATE している前例に沿う。
    起点は「検索の起点」なので、更新するのは検索系（B-6）と店詳細（B-7）だけ。
    失敗しても検索結果は返す（記録は付帯機能）。
    """
    lat_r, lng_r = round_origin(lat, lng)
    conn.execute(
        text(
            """
            UPDATE sessions
            SET origin_lat = :lat, origin_lng = :lng,
                origin_label = :label, origin_updated_at = now()
            WHERE id = :sid
            """
        ),
        {"lat": lat_r, "lng": lng_r, "label": label, "sid": sid},
    )
