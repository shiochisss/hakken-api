# ハッケンバス バックエンドAPI（hakken-api）

ハッケンバスMVPの**バックエンドAPI**を管理するリポジトリです。**FastAPI（Python）製**。設計書・DDLの正本は別リポジトリ（`hakken-docs`）にあり、フロントエンドは `hakken-front` にあります。

## 必要なもの

- **Python 3.11 以上**（https://www.python.org）
- Git、コードエディタ（**VS Code** 推奨）

## セットアップ

初回のみ、仮想環境（`.venv`＝このプロジェクト専用のPython部屋）を作り、ライブラリをインストールします。

- **Windows（PowerShell）**
  ```powershell
  py -m venv .venv
  .venv\Scripts\Activate.ps1
  pip install -r requirements.txt
  ```
- **Mac / Linux**
  ```bash
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
  ```

> `.venv\Scripts\Activate.ps1` が拒否される場合は、一度だけ `Set-ExecutionPolicy -Scope Process RemoteSigned` を実行してから再度Activateする。

## 起動方法

仮想環境を有効化した状態（プロンプト先頭に `(.venv)` が付いた状態）で実行します。

```bash
uvicorn main:app --reload --port 8000
```

- `Uvicorn running on http://127.0.0.1:8000` と出れば起動成功。
- `--reload` はコード変更時の自動再起動。ローカル開発用。
- 止めるときは **Ctrl + C**。

## 動作確認

起動したまま、ブラウザで次を開く。

| URL | 表示 |
|---|---|
| http://127.0.0.1:8000/docs | **Swagger UI**（APIの一覧・お試し実行画面） |
| http://127.0.0.1:8000/health | `{"status":"ok"}`（生存確認） |

## エンドポイント一覧

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/health` | 生存確認。`{"status":"ok"}` を返す |

※今後、API設計書（`hakken-docs/api/`）に沿ってエンドポイントを追加していく。

## 秘密情報の扱い

- DB接続文字列等は `.env`（Git管理外）に置く。**パスワード・接続文字列・APIキーは絶対にコミットしない**。
- `.env` は `.gitignore` 済み。GitHub上に `.env` が無いことを確認すること。
