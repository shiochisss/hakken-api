-- ============================================================
-- F1 ログインセッション（sessions）— hakken-api 実装所有
-- DB種別: Azure Database for PostgreSQL（2026-07-19 確定）
-- 位置づけ: ログインセッション・トークン管理は DB設計書スコープ外（実装に委ねる）。
--   そのため設計正本 hakken-docs/db/schema_postgres.sql とは分離し本ファイルで管理する。
-- 前提: schema_postgres.sql 適用後（users(id) を参照するため）に流す。
--   例) psql "<接続文字列>" -f db/001_sessions.sql
-- 方針:
--   - セッショントークンの生値は Cookie のみ。DBには SHA-256 hex を保存（漏洩時の奪取防止）。
--   - user_id は users.id と型一致（BIGINT）。退会は users.is_deleted で判定（認証チェック側）。
-- ============================================================

DROP TABLE IF EXISTS sessions CASCADE;

CREATE TABLE sessions (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    token_hash   CHAR(64) NOT NULL UNIQUE,                        -- 生トークンの SHA-256 hex（64桁）
    user_id      BIGINT   NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,                            -- 絶対有効期限（既定 now()+SESSION_TTL_DAYS）
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()               -- 監査・将来のスライディング延長用
);

CREATE INDEX idx_sessions_user    ON sessions (user_id);
CREATE INDEX idx_sessions_expires ON sessions (expires_at);       -- 期限切れ掃除用
