"""球モデルの距離計算 — 位置に関わる計算はすべてここの1定義を共有する。

これまで `app/routers/search.py` が `EARTH_R_M` と `_haversine_m` を持ち、`stores.py`・
`arrival.py` がそこから import していた。起点ラベル（`app/services/geocode.py`）でも同じ
距離計算が必要になり、そのまま search から import すると循環参照になるため、
**地球モデルだけを中立なモジュールに切り出した**。

search.py は後方互換のため従来名（`EARTH_R_M` / `_haversine_m`）で再公開しているので、
既存の import（`from app.routers.search import _haversine_m` 等）とテストは変更不要。

地球モデルを1箇所に固定しているのは意図的: 検索の円判定（`_nearby_walk1`）と矩形の
一次絞り込み（`_search_bbox`）で別の半径を使うと、円と矩形が微妙にズレて境界の停を
黙って取りこぼす（DB設計書9章#17・design文書 A-3）。
"""
from __future__ import annotations

import math

EARTH_R_M = 6371000.0  # 球モデルの半径（WGS84楕円体は使わない。全計算でこの値を共有する）


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """2点間の大円距離（m）。"""
    r = EARTH_R_M
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
