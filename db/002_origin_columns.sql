-- ============================================================
-- 起点（検索の現在地）の記録用カラム追加 — 2026-07-27
--
-- 背景: 実機テストで「現在地がどこからなのか分からないため、提示されるルートの信ぴょう性が
--   薄い」という指摘。S2 に起点の住所を出す（表示）とあわせて、あとから「その提案はどこ
--   起点だったか」を検証・分析できるように記録する（記録）。
--
-- 対象は2テーブル。**列追加のみ・NOT NULL 制約なし＝後方互換**なので、旧コードが動いている
-- 本番に先に流しても壊れない（作業ルール7「本番DDL → push」の順を守るために先に流す）。
--
--   例) psql "<接続文字列>" -f 002_origin_columns.sql
--
-- 冪等: ADD COLUMN IF NOT EXISTS なので何度流してもよい。
--
-- ⚠ 001_sessions.sql は冒頭で `DROP TABLE IF EXISTS sessions CASCADE` する新規構築用
--   スクリプトなので、**稼働中の本番に流してはいけない**（全ユーザーがログアウトする）。
--   既存DBへの反映は必ず本ファイルで行う。
--   なお 001 側の CREATE TABLE にも同じ列を追記済み＝新規構築時は本ファイル不要（流しても無害）。
--
-- ⚠ going_list は論理設計の正本が hakken-docs（DB設計書 3-4 ／ db/schema_postgres.sql）に
--   ある。本ファイルは「稼働中DBへ当てる操作用スクリプト」であり、正本側の CREATE TABLE
--   にも同じ4列を反映済み。作り直し時は schema_postgres.sql だけで足りる。
-- ============================================================

-- ---- 1. sessions（hakken-api 所有）: セッションの最新の検索起点 ----
-- 位置情報の生値は保存しない（DB設計書 1章-5）。**小数3桁＝約110m 格子に丸めた値**だけを
-- 持つ。NUMERIC(6,3) を使うのは、型そのものが粒度を保証する（実装ミスでも3桁より細かい値が
-- 入らない）ため。既存の stops.lat/lng が DOUBLE PRECISION なのとは意図的に別の型。
ALTER TABLE sessions
  ADD COLUMN IF NOT EXISTS origin_lat        NUMERIC(6,3),
  ADD COLUMN IF NOT EXISTS origin_lng        NUMERIC(6,3),
  ADD COLUMN IF NOT EXISTS origin_label      VARCHAR(120),
  ADD COLUMN IF NOT EXISTS origin_updated_at TIMESTAMPTZ;

COMMENT ON COLUMN sessions.origin_lat        IS '直近の検索起点の緯度（小数3桁に丸め・生値は保存しない）';
COMMENT ON COLUMN sessions.origin_lng        IS '直近の検索起点の経度（小数3桁に丸め・生値は保存しない）';
COMMENT ON COLUMN sessions.origin_label      IS '直近の検索起点の住所ラベル（町丁目まで。出典=大字・町丁目位置参照情報 国土交通省）';
COMMENT ON COLUMN sessions.origin_updated_at IS 'origin_* を最後に更新した時刻';

-- ---- 2. going_list（hakken-docs 所有）: 「ここ行く」宣言時点の起点 ----
-- sessions.origin_* は「そのセッションで最後に検索した場所」で上書きされ続け、かつ
-- ログアウト・期限切れ・退会で行ごと物理削除される。宣言時点の起点を確実に残すため、
-- going_list 側にコピーを持つ（POST /api/going が sessions から転記する）。
--
-- session_id を **FK にしない**理由（重要）:
--   sessions の行はログアウト（auth.py の DELETE）・期限切れ／退会（deps.py の DELETE）で
--   **物理削除**される。FK にすると
--     - 既定(RESTRICT) … ログアウトの DELETE が失敗して**ログアウトが壊れる**
--     - CASCADE        … going_list の行まで消えて**記録が消える**
--     - SET NULL       … session_id が NULL になり**分析用の情報が消える**
--   いずれも目的に反する。グルーピング分析には ID の値だけあれば足りるので、
--   参照整合性を持たない「記録用の値」として持つ。
ALTER TABLE going_list
  ADD COLUMN IF NOT EXISTS session_id   BIGINT,
  ADD COLUMN IF NOT EXISTS origin_lat   NUMERIC(6,3),
  ADD COLUMN IF NOT EXISTS origin_lng   NUMERIC(6,3),
  ADD COLUMN IF NOT EXISTS origin_label VARCHAR(120);

COMMENT ON COLUMN going_list.session_id   IS '宣言したセッションのid（FKなし＝sessionsは物理削除されるため。分析用）';
COMMENT ON COLUMN going_list.origin_lat   IS '宣言時点の起点の緯度（小数3桁に丸め）';
COMMENT ON COLUMN going_list.origin_lng   IS '宣言時点の起点の経度（小数3桁に丸め）';
COMMENT ON COLUMN going_list.origin_label IS '宣言時点の起点の住所ラベル';

-- ---- 確認 ----
-- \d sessions
-- \d going_list
