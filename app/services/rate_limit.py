"""レート制限のインターフェースのみを定義する。

本番用インメモリ実装はRouterへ組み込まない（複数インスタンスでカウンターが共有されず
本番要件を満たさないため）。正式なレート制限方式・値はTBD。
FakeRateLimiter はテスト専用であり、本番コードパスからは使用しない。

出典: hakken-f11 納品物（F11・おかむー）app/services/rate_limit.py を無改変で移植。
"""

from __future__ import annotations

from typing import Protocol


class RateLimiter(Protocol):
    def check(self, key: str) -> bool:
        """呼び出し可能ならTrue、レート制限に達しているならFalseを返す。"""
        ...


class FakeRateLimiter:
    """テスト専用。常に許可/常に拒否を明示的に切り替えられる。"""

    def __init__(self, *, allow: bool = True) -> None:
        self.allow = allow
        self.calls: list[str] = []

    def check(self, key: str) -> bool:
        self.calls.append(key)
        return self.allow
