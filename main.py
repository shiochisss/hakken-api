from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import config
from app.routers import (
    arrival,
    auth,
    conditions,
    events,
    favorites,
    going,
    me,
    mylist,
    photo_upload,
    search,
    stores,
    submissions,
)

app = FastAPI(title="ハッケンバス API")

# フロント（別オリジン）から Cookie 付きで叩くため、CORS は資格情報許可＋明示 origin。
# ワイルドカード（*）は allow_credentials と併用不可。
app.add_middleware(
    CORSMiddleware,
    allow_origins=[config.FRONTEND_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 入力エラーは 400 に統一する（F11 の RFP・技術回答が 400 契約。B-6 も「範囲外は 400」と
# 規定しており、search.py が Query(ge/le) の 422 を避けて手動 400 にしているのと同じ方針）。
# FastAPI/Pydantic の既定は 422 のため、RequestValidationError をここで 400 へ変換する。
# アプリ全体に適用する（既存9本の検証エラーも 422→400 になる。422 を期待するテスト・
# フロント実装は無いことを確認済み）。
@app.exception_handler(RequestValidationError)
async def validation_error_as_400(request: Request, exc: RequestValidationError) -> JSONResponse:
    # jsonable_encoder は Pydantic の内部エラー（ctx.error 等、非シリアライズ可能な例外を
    # 含みうる）を安全な文字列表現へ変換する。FastAPI 標準の 422 ハンドラも内部で同様に行う。
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": jsonable_encoder(exc.errors())},
    )


app.include_router(auth.router)
app.include_router(me.router)
app.include_router(conditions.router)  # F3 楽条件（B-4/B-5）
app.include_router(favorites.router)   # F6 お気に入り（B-8/B-9）
app.include_router(going.router)       # F7 ここ行く（B-10）
app.include_router(mylist.router)      # F7 マイリスト取得（B-11）
app.include_router(arrival.router)     # F8 着いたよ／着いたバナー（B-12/B-13）
app.include_router(search.router)      # F4 逆引き検索（B-6）
app.include_router(stores.router)      # F4 店詳細（B-7）
app.include_router(events.router)      # 計測イベント（B-14）
app.include_router(submissions.router)  # F11 たれ込み投稿（B-15）
app.include_router(photo_upload.router)  # F11 写真アップロード（B-16）


@app.get("/health")
def health():
    # 生存確認用。ここが返れば起動成功
    return {"status": "ok"}
