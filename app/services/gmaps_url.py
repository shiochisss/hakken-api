"""Googleマップ URL の形式検証。Google APIは呼び出さず、短縮URLも展開しない。
文字列としてそのまま payload JSONB へ保存する前提のバリデーションのみを行う。

出典: hakken-f11 納品物（F11・おかむー）app/services/gmaps_url.py を無改変で移植。
旧 app/routers/submissions.py::_is_gmaps_url（部分文字列一致）を置き換える。
旧実装は "google.com/maps" 等の部分一致だったため
`https://evil.example.com/?x=google.com/maps` のような偽装URLを通していた。
"""

from __future__ import annotations

from urllib.parse import urlsplit

# 許可ホスト（完全一致のみ。部分一致・サブストリングでの偽装ドメインは許可しない）。
# "google.com/maps" 等の記載は host=google.com かつ path が /maps で始まることを意味する。
_ALLOWED_HOST_RULES: tuple[tuple[str, str | None], ...] = (
    ("maps.app.goo.gl", None),
    ("maps.google.com", None),
    ("google.com", "/maps"),
    ("www.google.com", "/maps"),
)


def is_valid_google_maps_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False

    if parts.scheme != "https":
        return False
    if "@" in parts.netloc:  # userinfo（user:pass@host）を含むURLは拒否
        return False

    hostname = parts.hostname or ""
    for allowed_host, required_path_prefix in _ALLOWED_HOST_RULES:
        if hostname == allowed_host:
            if required_path_prefix is None or parts.path.startswith(required_path_prefix):
                return True
    return False
