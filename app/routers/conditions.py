"""GET/PUT /api/conditions — 楽条件の取得・保存（F3・API設計書 B-4/B-5）。

1ユーザー1セット。GET は未設定なら 404（フロントは /setup へ）。PUT は UPSERT。
enum・範囲の違反は 400（DB の CHECK 制約とも一致）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from app.db import get_engine
from app.deps import get_current_uid
from app.services.limits import validate_raku_max

router = APIRouter()

_TRANSFERS = {"none", "hub1"}
_PRESETS = {"no_walk", "balance", "far_ok", "custom"}
# 各上限の許容範囲（UIプリセットの具体値）は設計書 B-5 で TBD。ここでのセキュリティ上限
# （walk_max/ride_max/total_max の下限・上限）は B-6（検索）と共有する
# ＝app/services/limits.py（v1.7）。片方だけ緩いと「条件保存は通るのに検索は400」に
# なるため一元管理する。


class ConditionsIn(BaseModel):
    walk_max: int
    ride_max: int
    total_max: int
    transfer: str
    preset_key: str


def validate_conditions(c: "ConditionsIn") -> None:
    """ドメイン検証。違反は ValueError（呼び出し側で 400 に変換）。"""
    validate_raku_max(c.walk_max, c.ride_max, c.total_max)
    if c.transfer not in _TRANSFERS:
        raise ValueError("invalid transfer")
    if c.preset_key not in _PRESETS:
        raise ValueError("invalid preset_key")


def _as_dict(c: "ConditionsIn") -> dict:
    return {
        "walk_max": c.walk_max,
        "ride_max": c.ride_max,
        "total_max": c.total_max,
        "transfer": c.transfer,
        "preset_key": c.preset_key,
    }


@router.get("/api/conditions")
def get_conditions(uid: int = Depends(get_current_uid)):
    with get_engine().begin() as conn:
        row = conn.execute(
            text(
                """
                SELECT walk_max, ride_max, total_max, transfer, preset_key
                FROM user_conditions
                WHERE user_id = :uid
                """
            ),
            {"uid": uid},
        ).mappings().first()
    if row is None:
        # 初回（未設定）は 404 → フロントは /setup へ遷移（B-4）
        raise HTTPException(status_code=404, detail="conditions not set")
    return dict(row)


@router.put("/api/conditions")
def put_conditions(body: ConditionsIn, uid: int = Depends(get_current_uid)):
    try:
        validate_conditions(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO user_conditions
                  (user_id, walk_max, ride_max, total_max, transfer, preset_key, updated_at)
                VALUES (:uid, :walk_max, :ride_max, :total_max, :transfer, :preset_key, now())
                ON CONFLICT (user_id) DO UPDATE SET
                  walk_max = EXCLUDED.walk_max, ride_max = EXCLUDED.ride_max,
                  total_max = EXCLUDED.total_max, transfer = EXCLUDED.transfer,
                  preset_key = EXCLUDED.preset_key, updated_at = now()
                """
            ),
            {"uid": uid, **_as_dict(body)},
        )
    # 保存後の Conditions を返す（リクエストと同形）
    return _as_dict(body)
