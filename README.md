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
| GET | `/api/search` | 逆引き検索。楽な順に店を返す＋`meta.origin`（起点の住所）（F4・B-6） |
| GET | `/api/stores/{store_id}` | 店詳細（現在地からの楽さ内訳込み）（F4・B-7） |
| POST | `/api/going` | 「ここ行く」登録＋`koko_iku`計測→`{going_id}`（F7・B-10） |
| GET | `/api/mylist` | マイリスト（行く予定／お気に入り）（F7・B-11） |
| POST | `/api/going/{going_id}/arrived` | 着いたよ。150m以内→`verified`／遠い→`pending`（F8・B-12） |
| GET | `/api/arrival-banner` | 着いたバナー照合。該当なしは`null`（F8b・B-13） |
| POST | `/api/events` | 計測イベントを`event_log`へ追記→204（B-14） |
| POST | `/api/submissions` | たれ込み投稿を`pending`で受付→`{submission_id}`（F11・B-15） |
| POST | `/api/submissions/photo-upload` | たれ込み写真を非公開Blobへ→`{photo_id}`（F11・B-16） |

※**API設計書（B-1〜B-16）の全エンドポイントを実装済み**（2026-07-26 に B-12/B-13 を追加して完了）。B-17 は運営の手動DB操作でありオンラインAPIは無い。

## 認証（F1：Googleログイン）

- 方式：Google OAuth 2.0 Authorization Code フロー。**サーバセッション（`sessions`テーブル）** で管理し、
  セッショントークンは **HttpOnly Cookie**（生値）＋ **DBにはSHA-256ハッシュ**を保存。
- 保護：`/api/*` は要ログイン（`app/deps.py` の `get_current_uid`）。未認証・期限切れ・退会（`is_deleted`）は401。
- CSRF：`state` Cookie 照合。オープンリダイレクト防止：`next` は `FRONTEND_ORIGIN` 同一originのみ許可。
- **DBマイグレーション**：`schema_postgres.sql`（docs）適用後に `db/001_sessions.sql` を流す（`users(id)`参照のため順序必須）。稼働中のDBに列を足すときは `db/002_origin_columns.sql`（001 は `DROP TABLE` するので流さない）。
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
  stops 1,491→**6,087**／route_segments 23,937→108,069。
  拡大直後は検索応答が約2倍に悪化したが（stops を毎リクエスト全件ロードしていたため）、
  SQL側の矩形絞り込みで解消済み（**median 2.6-2.9ms**・ローカル計測。DB設計書9章#17）。
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
- **「本数少なめ」（trip_count）**：`route_segments.trip_count` に**その区間を土日10:00-16:00に走る便数**を
  持たせ、`reach.min_trip_count`（乗換ありは2区間の最小値）を経由して API が `few_trips: bool` を返す。
  検索からは**除外せず**、S2カード・S3詳細で「🚌 本数少なめ」バッジとして開示する（2026-07-26 判断。
  除外すると練馬駅起点で江古田の3店が消え、掲載16店に対して損失が大きいため）。
  しきい値は `batch/route_segments.py` の `FEW_TRIPS_THRESHOLD`（**2本未満・暫定**）。
- **reach の経路モデル**（2026-07-26 改訂・DB設計書9章#16）：直行（1区間）＋**乗換1回**（2区間）。
  **乗換停は任意の停**。旧実装は `is_hub` の停に限っていたが、実測でホワイトリストが狭すぎて
  **到達できる乗車停を1停も増やしていなかった**（713停のまま）。開放すると1,200停になり、
  江古田駅起点・歩かない条件でヒットが3店→8店に増える。`stops.is_hub` は残るが reach は参照しない。
  経路の優先順は「**ペナルティ込みの最短 → 直行 → 便数が多い**」。乗換ペナルティは
  `batch/reach.py` の `TRANSFER_PENALTY_MIN`（**3分・暫定**）で、**選択にだけ効き `ride_min` には
  含めない**（待ち時間を捨象しているモデルで「1分早いだけの乗換」が直行に勝つのを防ぐため）。
- テスト：`python -m tests.test_f9_stops`（stops パース層）／
  `python -m tests.test_route_segments`（区間・便数・reach の純関数）／
  `python -m tests.test_search_bbox`（検索の stops 一次絞り込み）／
  `python -m tests.test_arrival`（F8 の距離判定・バナー選択）。いずれも DB/ネットワーク不要。

## 起点（現在地）の住所表示と記録

実機テストで「**現在地がどこからなのか分からない**ため、提示される楽なルートの信ぴょう性が薄い」と
指摘されたことへの対応（2026-07-27）。S2 のヘッダに起点の住所を出し、あわせて
「その提案はどこ起点だったか」を後から辿れるように記録する。

- **住所は外部APIを呼ばない**。同梱した町丁目代表点（`config/oaza_points.json`・6,735点・412KB）の
  最寄り探索で解決する（`app/services/origin.py`）。追加ライブラリなし・障害点なし・所要 0.15ms。
  - 検討して見送った案: 国土地理院の逆ジオコーディングAPI（公式SLAが無く毎検索ぶら下がる）／
    `jageocoder`（辞書DBが最小 351MB(zip)で **App Service Free F1 のディスク1GB** と GitHub の
    100MB上限に載らない）／`reverse_geocoder`（市区町村・ローマ字で要件未達）。
  - 限界: 町丁目の**代表点**への最寄り判定なので境界付近では隣の町丁目名が出うる。国土地理院APIとの
    突き合わせ（12地点）で市区町村 12/12・町丁目 9/12 一致（DB設計書9章#18）。
- **位置の生値は保存しない**（DB設計書1章-5）。`round_origin` で**小数3桁（約110m格子）**に丸め、
  DB側も `NUMERIC(6,3)` で粒度を保証する（二重の防御）。
- 書き込み先: `GET /api/search`・`GET /api/stores/{id}` が `sessions.origin_*` を更新 →
  `POST /api/going` が `going_list` へ転記して宣言時点の起点を固定する。
  **`preview=1` では解決も記録もしない**（スライダー連打で無駄な処理をしないため）。
  `going_list.session_id` は **FK にしない**（sessions はログアウトで物理削除されるため）。
- **DDL**: `db/002_origin_columns.sql`（列追加のみ・`IF NOT EXISTS`・後方互換）。
  稼働中のDBにはこれを流す。**`001_sessions.sql` は冒頭で `DROP TABLE` するので本番に流さないこと**。
- **データの再生成**（年次更新の想定・手動）:
  ```bash
  python -m batch.build_oaza_points --dry-run   # 取得〜集計のみ
  python -m batch.build_oaza_points             # config/oaza_points.json を生成
  ```
  出典: **大字・町丁目位置参照情報 国土交通省**（`ISJ_VERSION = 19.0b`）をエリア矩形で抽出・加工。
  利用約款により商用利用可だが**出典明示が必須**（アプリ側は S5「位置情報について」に表示）。
  取得した zip は `data/isj/`（Git管理外）。
- 暫定値（すべて `app/services/origin.py` に定数化）:

| 値 | 定数 |
|---|---|
| 丸め桁数 3（≒110m格子） | `ORIGIN_PRECISION` |
| 格子1辺 0.01度（≒1.1km） | `GRID_DEG` |
| 探索の打ち切り 3x3 → 7x7（±3.3km） | `GRID_RINGS` |

- テスト: `python -m tests.test_origin`（丸めが必ず3桁／実地点の住所／**データが無くても例外を出さない**／
  ラベルが `VARCHAR(120)` に収まる）。DB・ネットワーク不要。

## 「歩いて○分」（walk_only）とS2/S3の一致

検索は `reach`（バス経路）しか見ていなかったため、**駅前の店にもバスを勧めていた**
（江古田駅→焼肉レストラン三宝苑は直線徒歩0分なのに「歩2＋バス2＋歩5＝9分」）。2026-07-28 に対応。

- `direct_walk + 1分 <= total` かつ `direct_walk <= walk_max` のとき `walk_only` を返し、
  **並び順も徒歩の時間で繰り上げる**（本番実測で37件が繰り上がり、最大 6位→1位）。
  該当率は本番146店・6起点×3プリセットで **8.6%**。
- **バス経路は消さない**（`raku`・`boarding_stop`・`route_label` はそのまま）。表示側が徒歩を主・バスを従にする。
- マージンは `app/routers/search.py` の `WALK_BEATS_BUS_MARGIN`（**1分・暫定**）。
  `direct_walk` は直線×1.3の**推定**で `total` は実測なので、同点で推定を勝たせない。
- 距離(m)も返すのは、表示で併記して**断定を避ける**ため（近似は迂回の大きい地形で外れる＝DB設計書9章#20）。

**S2 と S3 の所要時間が食い違う不具合もあわせて修正した。** B-7（店詳細）が
`walk_max`/`ride_max`/`total_max`/`transfer` を一切見ずに最小 total を選んでいたため、
同じ店が **S2 で29分（直行）／S3 で18分（乗換1回）** と食い違っていた（18分は
`transfer=none` の S2 が除外していた経路）。

- 条件は**クエリではなく `user_conditions` から読む**（API契約・フロントを変えずに済む）。
- 条件を満たす経路が1件も無いときは条件なしで最良を返し **`out_of_conditions`** で開示する
  （マイリスト・お気に入りから開いた店の情報を失わないため）。
- 本番実測：3起点×3プリセット・**402件すべてで S2 と S3 が一致**（修正前は不一致あり）。

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
