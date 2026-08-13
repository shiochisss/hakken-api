"""セキュリティ対策3件のテスト（API設計書 v1.7・A-10/A-11・B-5/B-6）。

DB/ネットワーク非依存の範囲のみ（他の test_*.py と同じ流儀）。
- Swagger非公開（A-11）：既定でdocs系ルートが無いこと（TestClientで到達確認）
- 検索条件の上限（B-5/B-6共有・app/services/limits.py）：純関数の境界値
- レート制限（A-10・app/services/rate_limit.py）：暫定値の固定＋DB呼び出し箇所の存在確認
  （COUNTクエリ自体はDBが要るため実DBでの手動確認に回す。他ファイルのDB到達コードと同じ扱い）

実行: (.venv 有効化後)  python -m tests.test_security_hardening
"""
from __future__ import annotations

import inspect
import os

os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-secret")
os.environ.setdefault("FRONTEND_ORIGIN", "http://localhost:3000")
os.environ.setdefault("OAUTH_REDIRECT_URI", "http://localhost:8000/auth/google/callback")
# 既定（非公開）の挙動を確定的に検証するため、ローカル .env の ENABLE_API_DOCS=true
# （README推奨のローカル開発設定）を上書きする。setdefault ではなく明示的な代入が必要
# （load_dotenv は既に設定済みの環境変数を上書きしない＝override=False が既定）。
os.environ["ENABLE_API_DOCS"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.routers import photo_upload, submissions  # noqa: E402
from app.services import limits  # noqa: E402
from app.services import rate_limit  # noqa: E402
from main import app  # noqa: E402

client = TestClient(app)


# ---- A-11: Swagger／APIドキュメントの公開制御 ----

def test_docs_disabled_by_default():
    """ENABLE_API_DOCS を設定しない実行では docs 系ルートが存在しないこと（フェイルセーフ）。"""
    assert config.ENABLE_API_DOCS is False, "テスト実行で誤って有効化されている"
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None


def test_docs_routes_are_unreachable_by_default():
    """/docs・/redoc・/openapi.json はルート自体が無いため 404（隠すのではなく無くす）。"""
    for path in ("/docs", "/redoc", "/openapi.json"):
        r = client.get(path)
        assert r.status_code == 404, f"{path} -> {r.status_code}（ENABLE_API_DOCS=false のはず）"


def test_health_is_unaffected():
    """生存確認用の /health は対象外＝据え置きで 200 のまま。"""
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ---- B-5/B-6: 検索条件のセキュリティ上限（共有: app/services/limits.py） ----

def test_raku_ceilings_are_provisional_values():
    """暫定値が変わったらAPI設計書v1.7（A-10近傍・B-5/B-6）の記述も直すこと。"""
    assert limits.RAKU_MIN == 1
    assert limits.WALK_MAX_CEILING == 60
    assert limits.RIDE_MAX_CEILING == 90
    assert limits.TOTAL_MAX_CEILING == 150


def test_validate_raku_max_accepts_within_range():
    limits.validate_raku_max(1, 1, 1)                       # 下限ちょうど
    limits.validate_raku_max(60, 90, 150)                   # 上限ちょうど
    limits.validate_raku_max(15, 20, 40)                    # 通常値


def test_validate_raku_max_rejects_below_min():
    for bad in (dict(walk_max=0, ride_max=20, total_max=40),
                dict(walk_max=15, ride_max=0, total_max=40),
                dict(walk_max=15, ride_max=20, total_max=0)):
        try:
            limits.validate_raku_max(**bad)
            raise AssertionError(f"should have raised for {bad}")
        except ValueError:
            pass


def test_validate_raku_max_rejects_above_ceiling():
    """イタズラで巨大な値を送るケース。境界+1で弾かれること。"""
    for bad in (dict(walk_max=61, ride_max=20, total_max=40),
                dict(walk_max=15, ride_max=91, total_max=40),
                dict(walk_max=15, ride_max=20, total_max=151),
                dict(walk_max=999999999, ride_max=20, total_max=40)):
        try:
            limits.validate_raku_max(**bad)
            raise AssertionError(f"should have raised for {bad}")
        except ValueError:
            pass


def test_search_and_conditions_share_the_same_ceilings():
    """B-6(search.py)・B-5(conditions.py)が同じ検証関数を呼んでいること（上限のドリフト防止）。"""
    from app.routers import conditions, search

    assert "validate_raku_max" in inspect.getsource(search.search)
    assert "validate_raku_max" in inspect.getsource(conditions.validate_conditions)


# ---- A-10: レート制限（B-15/B-16） ----

def test_rate_limit_values_are_provisional_values():
    """暫定値（標準プロファイル）が変わったらAPI設計書v1.7のA-10も直すこと。"""
    assert rate_limit.SUBMISSIONS_WINDOW_MINUTES == 10
    assert rate_limit.SUBMISSIONS_LIMIT == 5
    assert rate_limit.PHOTO_HOURLY_LIMIT == 10
    assert rate_limit.PHOTO_DAILY_LIMIT == 30


def test_submissions_router_checks_rate_limit_before_insert():
    """B-15: レート制限判定がINSERTより前にあること（ソース上の出現順で確認）。"""
    src = inspect.getsource(submissions.create_submission)
    check_pos = src.index("check_submissions_rate_limit")
    insert_pos = src.index("insert_submission(")
    assert check_pos < insert_pos, "レート制限判定はINSERTより前に行うこと（A-10）"


def test_photo_upload_router_checks_rate_limit_before_processing():
    """B-16: レート制限判定がファイル読込・画像処理・Blob書込より前にあること。

    超過分の処理コスト・Azure Blob課金を発生させないための順序（A-10）。
    """
    src = inspect.getsource(photo_upload.upload_photo)
    check_pos = src.index("check_photo_upload_rate_limit")
    read_pos = src.index("_read_with_limit(")
    upload_pos = src.index("service.upload(")
    assert check_pos < read_pos < upload_pos


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok: {t.__name__}")
    print(f"全 {len(tests)} テスト成功")


if __name__ == "__main__":
    main()
