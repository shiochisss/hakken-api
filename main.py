from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import config
from app.routers import auth, me

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


@app.get("/health")
def health():
    # 生存確認用。ここが返れば起動成功
    return {"status": "ok"}
