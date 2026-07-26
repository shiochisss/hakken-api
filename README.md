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

### 本番起動（Azure App Service）

本番（`hakken-bus-api` / Production スロット）は、Azure Portal「構成 → スタック設定」の
**スタートアップコマンド**に次を設定して起動している。

```bash
gunicorn -w 2 -k uvicorn.workers.UvicornWorker main:app --forwarded-allow-ips="*"
```

- `-k uvicorn.workers.UvicornWorker`：ASGI（FastAPI）を gunicorn 上で動かすワーカー。
- `--forwarded-allow-ips="*"`：Azure はプロキシで TLS 終端するため、`X-Forwarded-*` を
  信頼して scheme／クライアントIPを判定させる（Secure Cookie・HTTPS 判定に必須。
  下記「認証」節の `--proxy-headers` 相当）。
- `-w 2`：ワーカープロセス数。

> ⚠️ **この設定は Azure Portal の構成でのみ管理されており、リポジトリには反映されない。**
> スタートアップコマンドを変更したときは、この README も必ず更新すること。

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
| GET | `/auth/google/login` | Googleログイン開始→認可画面へ302（F1・B-2） |
| GET | `/auth/google/callback` | Googleコールバック→users保存/照合→セッション発行→`next`へ302（F1・B-2） |
| POST | `/auth/logout` | セッション破棄→204（F1・B-3） |
| GET | `/api/me` | ログイン中ユーザー`{id,email,has_conditions}`／未ログインは401（F1・B-1） |
| GET | `/api/conditions` | 楽条件を取得。未設定は404（F3・B-4） |
| PUT | `/api/conditions` | 楽条件をUPSERT保存し保存後の値を返す（F3・B-5） |
| POST | `/api/favorites` | お気に入り追加（重複は冪等・204）（F6・B-8） |
| DELETE | `/api/favorites/{store_id}` | お気に入り解除（未登録も冪等・204）（F6・B-9） |
| POST | `/api/going` | 「ここ行く」登録＋`koko_iku`計測→`{going_id}`（F7・B-10） |
| POST | `/api/events` | 計測イベントを`event_log`へ追記→204（B-14） |
| POST | `/api/submissions` | たれ込み投稿を`pending`で受付→`{submission_id}`（F11・B-15） |

※今後、API設計書（`hakken-docs/api/`）に沿ってエンドポイントを追加していく。未実装：検索`/api/search`・店詳細`/api/stores/{id}`（B-6/B-7＝reach事前計算と探索半径が未決）、マイリスト`/api/mylist`（B-11）、着いた系（B-12/B-13）、写真アップロード（B-16＝Blob構築待ち）。

## 認証（F1：Googleログイン）

- 方式：Google OAuth 2.0 Authorization Code フロー。**サーバセッション（`sessions`テーブル）** で管理し、
  セッショントークンは **HttpOnly Cookie**（生値）＋ **DBにはSHA-256ハッシュ**を保存。
- 保護：`/api/*` は要ログイン（`app/deps.py` の `get_current_uid`）。未認証・期限切れ・退会（`is_deleted`）は401。
- CSRF：`state` Cookie 照合。オープンリダイレクト防止：`next` は `FRONTEND_ORIGIN` 同一originのみ許可。
- **DBマイグレーション**：`schema_postgres.sql`（docs）適用後に `db/001_sessions.sql` を流す（`users(id)`参照のため順序必須）。
- **Azure（本番）注意**：TLSはプロキシ終端のため、Secure Cookie/scheme判定に `--proxy-headers`（uvicorn）等を有効化する（実際の設定は「起動方法 → 本番起動（Azure App Service）」節参照）。
- テスト：`python -m tests.test_auth`（DB/Google非依存の到達パス＋純関数）。

## 夜間バッチ（F9：バス停DB生成）

GTFS-JP から `stops` テーブルを生成するバッチ。詳細は DB設計書 4-2／要件定義書 F9 を参照。

```bash
# .venv 有効化後
python -m batch.f9_stops --dry-run   # DB書込なしで取得〜パース〜サマリのみ
python -m batch.f9_stops             # 本反映
```

- 処理：**5社（西武・都営・関東・京王・京成トランジット）** の GTFS zip を ODPT から取得 →
  `stops.txt` をエリア矩形＋`location_type=0` でフィルタ → `gtfs_stop_id`（`社:原ID`）をキーに
  UPSERT → is_hub をホワイトリストで付与。**新規依存なし**（標準ライブラリ＋既存 SQLAlchemy）。
- **エリア矩形**：2026-07-26 に**東京23区相当**へ拡大（`config/area_bbox.json`）。
  stops 1,491→6,880／route_segments 23,937→108,069／検索応答は約2.2倍（41→102ms・ローカル計測）。
- **対象から外した社**：小田急（`calendar.txt` 欠落で区間が常に0件）・西東京（23区内に停0件）。
  経緯と復活条件は `config/gtfs_sources.json` の `"_removed_2026-07-26"` に記録。
- **国際興業バスは取得不可**：ODPT・gtfs-data.jp のいずれにも GTFS が存在しない（2026-07-26 確認）。
  練馬駅→江古田駅を直接結ぶ経路（高60系統等）は表現できない。
- **列長ガード**：DBに入れる前に列長をチェックし長さエラー（PostgreSQL 22001「value too long」）を予防。
  `gtfs_stop_id` は切詰不可のため上限超はスキップ、`name` は上限で切詰（件数はサマリに出力）。
  上限は `batch/f9_stops.py` の `MAX_GTFS_STOP_ID`／`MAX_STOP_NAME`（DDL準拠：PostgreSQL）。
- **DB**：Azure Database for PostgreSQL に確定（2026-07-19）。`db.py` が `postgresql://` を
  psycopg(v3) 用に正規化。`--dry-run` はDBに触れず動作確認できる。
- 設定（値は仮置き・後で差し替え）：`config/area_bbox.json`（エリア矩形）／
  `config/hub_stops.json`（is_hub）／`config/gtfs_sources.json`（取得元URL・社別）。
- 取得した zip は `data/gtfs/`（Git管理外）に置かれる。
- テスト：`python -m tests.test_f9_stops`（パース層・DB/ネットワーク不要）。
- `routes`／`route_segments` の取込は後続で追加予定。

## 業務API（F3楽条件／F6お気に入り／F7ここ行く／計測／F11たれ込み）

- 実装：`app/routers/` に機能ごと（`conditions.py`／`favorites.py`／`going.py`／`events.py`／`submissions.py`）。
  すべて `Depends(get_current_uid)` で要ログイン（未認証・期限切れ・退会は401）。DB は生SQL（`text()`）。
- **冪等**：お気に入り追加は重複を `DO NOTHING`→204、解除は未登録でも204（B-8/B-9）。楽条件PUTはUPSERT。
- **検証**：enum/範囲/たれ込みのドメイン違反は **400**（DBの CHECK 制約とも一致）。`store_id` 不明は404。
  計測 `event_type` は8種のenum内のみ許可（不正値でDB 500になるのを防ぐ）。
- **計測の記録主体**：`koko_iku` は `POST /api/going` がサーバ側で記録（フロントは送らない・二重計上なし）。
- **DB前提**：`schema_postgres.sql`（docs）適用済みであること（`user_conditions`／`favorites`／`going_list`／`event_log`／`submissions`）。
- テスト：`python -m tests.test_endpoints`（純関数の検証＋未ログイン401の到達パス。DB/ネットワーク不要）。

## 秘密情報の扱い

- DB接続文字列・**ODPTアクセストークン**等は `.env`（Git管理外）に置く。**パスワード・接続文字列・APIキー/トークンは絶対にコミットしない**。
- `.env` は `.gitignore` 済み。GitHub上に `.env` が無いことを確認すること。
- バッチが使う環境変数：
  - `DATABASE_URL`（必須）
  - `ODPT_CONSUMER_KEY`（必須。ODPTのアクセストークン。コード・config・URLに直書きしない）
  - `KANTO_GTFS_DATE`（任意。関東バスの `date` を明示指定。未指定なら当日を使用。有効期間切れ時は取得エラー）
- F1認証が使う環境変数（**本番は Azure App Service の「アプリ設定」に登録**。`.env`はデプロイに乗らない）：
  - `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`（必須。GoogleのOAuthクライアント）
  - `OAUTH_REDIRECT_URI`（既定=ローカル。本番は `https://hakken-bus-api-....azurewebsites.net/auth/google/callback`）
  - `FRONTEND_ORIGIN`（CORS/next許可/既定リダイレクト先。ローカルは `http://localhost:3000`）
  - `SESSION_COOKIE_SECURE`（本番 `true`）／`SESSION_COOKIE_SAMESITE`（本番 `none`・ローカル `lax`）
  - `SESSION_TTL_DAYS`（任意・既定30）
