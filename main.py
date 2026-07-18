from fastapi import FastAPI

app = FastAPI(title="ハッケンバス API")


@app.get("/health")
def health():
    # 生存確認用。ここが返れば起動成功
    return {"status": "ok"}
