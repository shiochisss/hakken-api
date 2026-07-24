"""軽量CRUDエンドポイントのテスト（DB 非依存）。

- 純関数（条件・たれ込みのドメイン検証、gmaps URL 判定）
- TestClient で「DB に触れない到達パス」＝未ログインは 401
  （Cookie 無しなら get_current_uid が DB 到達前に 401 を投げる）
実行: (.venv 有効化後)  python -m tests.test_endpoints
"""
from __future__ import annotations

import os

# app.config が import 時に env を読むため、先に設定する
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-secret")
os.environ.setdefault("FRONTEND_ORIGIN", "http://localhost:3000")
os.environ.setdefault("OAUTH_REDIRECT_URI", "http://localhost:8000/auth/google/callback")

from fastapi.testclient import TestClient  # noqa: E402

from app.routers.conditions import ConditionsIn, validate_conditions  # noqa: E402
from app.routers.submissions import _is_gmaps_url, validate_submission  # noqa: E402
from app.routers.events import _EVENT_TYPES  # noqa: E402
from main import app  # noqa: E402

client = TestClient(app)


def _cond(**kw) -> ConditionsIn:
    base = dict(walk_max=15, ride_max=20, total_max=40, transfer="none", preset_key="balance")
    base.update(kw)
    return ConditionsIn(**base)


# ---- 純関数：楽条件の検証（B-5） ----

def test_validate_conditions_ok():
    validate_conditions(_cond())  # 例外なし


def test_validate_conditions_bad_enum():
    for bad in (_cond(transfer="both"), _cond(preset_key="turbo")):
        try:
            validate_conditions(bad)
            raise AssertionError("should have raised")
        except ValueError:
            pass


def test_validate_conditions_out_of_range():
    for bad in (_cond(walk_max=0), _cond(ride_max=999), _cond(total_max=-1)):
        try:
            validate_conditions(bad)
            raise AssertionError("should have raised")
        except ValueError:
            pass


# ---- 純関数：たれ込みの検証（B-15） ----

def test_is_gmaps_url():
    assert _is_gmaps_url("https://www.google.com/maps/place/x")
    assert _is_gmaps_url("https://maps.app.goo.gl/abc")
    assert not _is_gmaps_url("http://google.com/maps")  # https 必須
    assert not _is_gmaps_url("https://example.com/x")


def test_validate_submission_new_store():
    validate_submission("new_store", None, {"gmaps_url": "https://maps.app.goo.gl/x", "comment": "良い店"})
    # store_id を付けてはいけない
    _expect_raise("new_store", 5, {"gmaps_url": "https://maps.app.goo.gl/x"})
    # gmaps_url 無し
    _expect_raise("new_store", None, {"comment": "x"})


def test_validate_submission_info_edit_and_closure():
    validate_submission("info_edit", 10, {"comment": "住所が違う"})
    validate_submission("closure_report", 10, {"reason": "貼り紙あり"})
    _expect_raise("info_edit", None, {"comment": "x"})   # store_id 必須
    _expect_raise("info_edit", 10, {"comment": "  "})     # 本文必須
    _expect_raise("closure_report", 10, {})               # reason 必須
    _expect_raise("bogus_type", 10, {})                   # type 不正


def _expect_raise(type_, store_id, payload):
    try:
        validate_submission(type_, store_id, payload)
        raise AssertionError(f"should have raised for {type_}")
    except ValueError:
        pass


def test_event_types_match_db_check():
    # DB の CHECK と件数一致（欠落・余剰の早期検知）
    assert len(_EVENT_TYPES) == 8
    assert "koko_iku" in _EVENT_TYPES and "app_open" in _EVENT_TYPES


# ---- 到達パス：未ログインは 401（DB に触れない） ----
# POST は妥当なボディを送り「認証だけが失敗する」状態にして 401 を確認する。

def test_requires_auth():
    cases = [
        ("get", "/api/conditions", None),
        ("put", "/api/conditions", _cond().model_dump()),
        ("get", "/api/mylist", None),
        ("get", "/api/search?lat=35.7&lng=139.65&walk_max=15&ride_max=20&total_max=40", None),
        ("get", "/api/stores/1?lat=35.7&lng=139.65", None),
        ("post", "/api/favorites", {"store_id": 1}),
        ("delete", "/api/favorites/1", None),
        ("post", "/api/going", {"store_id": 1}),
        ("post", "/api/events", {"event_type": "app_open"}),
        ("post", "/api/submissions", {"type": "closure_report", "store_id": 1, "payload": {"reason": "x"}}),
    ]
    for method, path, body in cases:
        r = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
        assert r.status_code == 401, f"{method.upper()} {path} -> {r.status_code} (expected 401)"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok: {t.__name__}")
    print(f"全 {len(tests)} テスト成功")


if __name__ == "__main__":
    main()
