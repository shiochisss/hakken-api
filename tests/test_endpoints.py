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
from pydantic import ValidationError  # noqa: E402

from app.routers.conditions import ConditionsIn, validate_conditions  # noqa: E402
from app.routers.events import _EVENT_TYPES  # noqa: E402
# B-15 の検証は F11 納品版へ差し替え済み。旧 submissions.py の
# _is_gmaps_url / validate_submission は廃止され、pydantic スキーマ＋
# services.gmaps_url に移った。
from app.schemas.submission import SubmissionIn  # noqa: E402
from app.services.gmaps_url import is_valid_google_maps_url  # noqa: E402
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


# ---- 純関数：たれ込みの検証（B-15・F11 納品版） ----

def test_is_valid_google_maps_url():
    assert is_valid_google_maps_url("https://www.google.com/maps/place/x")
    assert is_valid_google_maps_url("https://maps.app.goo.gl/abc")
    assert is_valid_google_maps_url("https://maps.google.com/?q=x")
    assert not is_valid_google_maps_url("http://google.com/maps")   # https 必須
    assert not is_valid_google_maps_url("https://example.com/x")
    # ホスト名は完全一致。旧実装（部分文字列一致）が通していた偽装URLを拒否する
    assert not is_valid_google_maps_url("https://evil.example.com/?x=google.com/maps")
    assert not is_valid_google_maps_url("https://google.com.evil.test/maps")
    assert not is_valid_google_maps_url("https://user:pass@maps.google.com/")  # userinfo 拒否
    assert not is_valid_google_maps_url("https://www.google.com/search?q=x")   # /maps 以外


def test_submission_new_store():
    SubmissionIn(
        type="new_store",
        store_id=None,
        payload={"gmaps_url": "https://maps.app.goo.gl/x", "comment": "良い店"},
    )
    # store_id を付けてはいけない
    _expect_raise("new_store", 5, {"gmaps_url": "https://maps.app.goo.gl/x"})
    # gmaps_url 無し
    _expect_raise("new_store", None, {"comment": "x"})
    # gmaps_url が Google マップでない
    _expect_raise("new_store", None, {"gmaps_url": "https://example.com/x"})


def test_submission_info_edit_and_closure():
    SubmissionIn(type="info_edit", store_id=10, payload={"comment": "住所が違う"})
    SubmissionIn(type="closure_report", store_id=10, payload={"reason": "貼り紙あり"})
    _expect_raise("info_edit", None, {"comment": "x"})   # store_id 必須
    _expect_raise("info_edit", 10, {"comment": "  "})     # 本文必須
    _expect_raise("closure_report", 10, {})               # reason 必須
    _expect_raise("bogus_type", 10, {})                   # type 不正


def test_submission_rejects_unknown_keys():
    """extra="forbid"：未知キーは黙って無視せず拒否する（user_id なりすまし対策）。"""
    # payload 内の未知キー
    _expect_raise("closure_report", 10, {"reason": "x", "evil": 1})
    # トップレベルの未知キー（user_id の詐称）
    try:
        SubmissionIn(
            type="closure_report", store_id=10, payload={"reason": "x"}, user_id=999
        )
        raise AssertionError("should have raised for unknown top-level key")
    except ValidationError:
        pass


def _expect_raise(type_, store_id, payload):
    try:
        SubmissionIn(type=type_, store_id=store_id, payload=payload)
        raise AssertionError(f"should have raised for {type_}")
    except ValidationError:
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
        ("post", "/api/going/1/arrived", {"lat": 35.7, "lng": 139.65}),
        ("get", "/api/arrival-banner?lat=35.7&lng=139.65", None),
        ("post", "/api/events", {"event_type": "app_open"}),
        ("post", "/api/submissions", {"type": "closure_report", "store_id": 1, "payload": {"reason": "x"}}),
        # B-16 写真アップロード。Depends(get_current_uid) はボディ検証より先に解決されるため、
        # multipart を付けなくても 401 になる。
        ("post", "/api/submissions/photo-upload", None),
    ]
    for method, path, body in cases:
        r = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
        assert r.status_code == 401, f"{method.upper()} {path} -> {r.status_code} (expected 401)"


def _dist_for(minutes: float) -> float:
    """`_walk_min` がちょうど `minutes` 分と丸める距離（m）を作る。"""
    from app.routers.search import WALK_DETOUR, WALK_SPEED_M_PER_MIN

    return minutes * WALK_SPEED_M_PER_MIN / WALK_DETOUR


def test_walk_only_margin_boundary():
    """マージンの境界。`direct + 1 <= total` のときだけ徒歩にする。

    2026-07-28 追加。それまで検索は reach（バス経路）しか見ておらず、駅前の店にも
    バスを勧めていた（江古田駅→焼肉レストラン三宝苑＝直線徒歩0分に「歩2＋バス2＋歩5＝9分」）。
    """
    from app.routers.search import WALK_BEATS_BUS_MARGIN, walk_only_info

    assert WALK_BEATS_BUS_MARGIN == 1, "マージンを変えたら設計書9章の暫定値も直すこと"

    d5 = _dist_for(5)
    # 徒歩5分 vs バス6分 → 5+1<=6 で徒歩
    got = walk_only_info(d5, total=6, walk_max=20)
    assert got is not None and got["minutes"] == 5
    assert got["distance_m"] == int(round(d5))
    # 徒歩5分 vs バス5分（同点）→ 推定を実測に勝たせない
    assert walk_only_info(d5, total=5, walk_max=20) is None
    # 徒歩5分 vs バス4分（バスが速い）→ 当然バス
    assert walk_only_info(d5, total=4, walk_max=20) is None


def test_walk_only_respects_walk_max():
    """歩き上限を超える徒歩には置き換えない（「歩かない」設定を破らない）。"""
    from app.routers.search import walk_only_info

    d12 = _dist_for(12)
    # 上限20分なら採用
    assert walk_only_info(d12, total=30, walk_max=20) is not None
    # 上限10分（no_walk）なら不採用＝バス経路のまま残る
    assert walk_only_info(d12, total=30, walk_max=10) is None


def test_walk_only_zero_minutes_is_valid():
    """店が目の前（徒歩0分）でも採用されること。

    実測で最も不合理だったのがこのケース（江古田駅→三宝苑は直線20m＝徒歩0分なのに
    バス9分を提示していた）。0 は falsy なので判定から漏れやすい＝明示的に固定する。
    """
    from app.routers.search import walk_only_info

    got = walk_only_info(_dist_for(0.2), total=9, walk_max=10)
    assert got is not None and got["minutes"] == 0


def test_stores_walk_only_fallback_bound():
    """walk_only の歩き上限は user_conditions.walk_max。未設定時のみこの値。"""
    from app.routers.stores import WALK_ONLY_MAX_MIN

    assert WALK_ONLY_MAX_MIN == 20, "変えたら API設計書 B-7 の記述も直すこと"


def test_stores_applies_user_conditions():
    """B-7 が楽条件を読むこと（S2 と S3 の所要時間が食い違わないための担保）。

    2026-07-28 まで B-7 は walk_max/ride_max/total_max/transfer を一切見ずに最小 total を
    選んでいた。そのため同じ店が **S2 で29分（直行）／S3 で18分（乗換1回）** と食い違った
    （18分は「乗換なし」設定の S2 が除外していた経路）。条件はクエリではなく
    user_conditions から読む（API契約・フロントを変えずに済むため）。
    """
    import inspect

    from app.routers import stores as m

    src = inspect.getsource(m.get_store)
    assert "FROM user_conditions" in src, "B-7 が楽条件を読んでいない＝S2 と食い違う"
    for key in ("transfer", "walk_max", "ride_max", "total_max"):
        assert key in src, f"{key} を条件判定に使っていない"
    assert "out_of_conditions" in src, "条件を満たす経路が無いときの開示が無い"


def test_category_keys_match_front_chips():
    """サーバの対応表とフロントのチップのキーが一致していること。

    2026-07-28: それまで search.py は category を受け取るだけで**絞り込みに使っておらず**、
    どのチップを押しても全件が返っていた（本番で発見）。
    `bakery`・`sento` は掲載データでは該当0件だが、**キーは残して「押すと正しく0件」に
    する**方針（DB設計書9章#12）。サーバが未知のキーを400で弾くため、フロントの
    CATEGORY_CHIPS とキー集合が食い違うと検索が失敗する。ここで固定して取り違えを防ぐ。
    """
    from app.routers.search import _CATEGORY_SQL

    assert set(_CATEGORY_SQL) == {"cafe", "food", "bakery", "sento"}, (
        "チップを増減したら hakken-front の CATEGORY_CHIPS も合わせること"
    )


def test_category_food_keeps_null_category_s():
    """「ごはん」の条件が category_s の NULL を落とさないこと。

    設計書の SQL は `s.category_s <> 'パン'` だが、NULL <> 'パン' は NULL 判定になり、
    category_s が未設定の店（本番実測5店）が「ごはん」から消える。coalesce が必須。
    """
    from app.routers.search import _CATEGORY_SQL

    food = _CATEGORY_SQL["food"]
    assert "coalesce" in food.lower(), f"NULL 落ちを防ぐ coalesce が無い: {food}"


def test_search_rejects_unknown_category():
    """対応表に無い category は 400（黙って全件返すと絞れたように見えて気付けない）。"""
    r = client.get("/api/search", params={
        "lat": 35.7357, "lng": 139.6518,
        "walk_max": 15, "ride_max": 20, "total_max": 40, "transfer": "none",
        "category": "ramen",   # 対応表に無いキー
    })
    # 未ログインなら 401 が先に立つ。Cookie 無しの到達パスでは 401、認証済みなら 400。
    assert r.status_code in (400, 401), r.status_code


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok: {t.__name__}")
    print(f"全 {len(tests)} テスト成功")


if __name__ == "__main__":
    main()
