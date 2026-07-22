from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import config
from app.routers import auth, conditions, events, favorites, going, me, mylist, submissions

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

app.include_router(auth.router)
app.include_router(me.router)
app.include_router(conditions.router)  # F3 楽条件（B-4/B-5）
app.include_router(favorites.router)   # F6 お気に入り（B-8/B-9）
app.include_router(going.router)       # F7 ここ行く（B-10）
app.include_router(mylist.router)      # F7 マイリスト取得（B-11）
app.include_router(events.router)      # 計測イベント（B-14）
app.include_router(submissions.router)  # F11 たれ込み投稿（B-15）


@app.get("/health")
def health():
    # 生存確認用。ここが返れば起動成功
    return {"status": "ok"}
