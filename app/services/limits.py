"""楽条件（walk_max / ride_max / total_max）の許容範囲。

B-5（PUT /api/conditions・条件保存）と B-6（GET /api/search・検索）が同じ値を
参照する単一のソース。片方だけ緩い上限にすると「条件保存は通るのに検索は400になる」
という矛盾が起きるため、ここで一元管理する（API設計書 v1.7・B-5/B-6）。

これは UI のプリセット実用域（スライダーの適正範囲。B-5 未決事項）とは別の
**セキュリティ上限**。イタズラで巨大な値を送りクエリ負荷をかけることを防ぐための
安全弁であり、正式なプリセット値が別途決まってもここは緩めない想定。

【2026-08-14 判断】旧上限（`_MIN,_MAX=1,240`・conditions.py）の間で直接APIを叩く等して
`user_conditions` に保存済みの行があると、この変更後は GET /api/search が新上限で400を
返す（該当ユーザーの検索が壊れうる）。データのクランプ（マイグレーション）は今回行わない
＝現状の公開範囲は知人限定の検証運用のみで、該当した場合は直接問い合わせが来る想定のため
（本番ユーザー影響は許容してエラーのまま運用し、必要になれば個別対応する）。
"""
from __future__ import annotations

RAKU_MIN = 1
WALK_MAX_CEILING = 60
RIDE_MAX_CEILING = 90
TOTAL_MAX_CEILING = 150

_CEILINGS = {
    "walk_max": WALK_MAX_CEILING,
    "ride_max": RIDE_MAX_CEILING,
    "total_max": TOTAL_MAX_CEILING,
}


def validate_raku_max(walk_max: int, ride_max: int, total_max: int) -> None:
    """walk_max/ride_max/total_max が [RAKU_MIN, 各セキュリティ上限] に収まるか検証する。

    違反は ValueError（呼び出し側で 400 に変換する）。DB非依存の純関数。
    """
    for name, v in (("walk_max", walk_max), ("ride_max", ride_max), ("total_max", total_max)):
        ceiling = _CEILINGS[name]
        if not (RAKU_MIN <= v <= ceiling):
            raise ValueError(f"{name} must be between {RAKU_MIN} and {ceiling}")
