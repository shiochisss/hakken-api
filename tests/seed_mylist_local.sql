-- ============================================================
-- seed_mylist_local.sql — GET /api/mylist（B-11）ローカル検証用シード
-- ★ hakken_local 専用。本番DBには絶対に流さないこと。★
--
-- 既知トークン 'mylist-local-test-token' の SHA-256 を sessions に格納するので、
-- 検証時は次の Cookie で叩ける:
--   Cookie: hakken_session=mylist-local-test-token
--
-- 冪等：先頭で本シード分（google_sub='test-sub-mylist' / place_id='test-place-*'）を
-- 掃除してから入れ直す。何度流しても同じ状態になる。
--
-- 検証観点（4点）:
--   ② テスト店A は going を2タップ→DISTINCT ONで最新1件に畳まれる
--   降順  going は tapped_at DESC（A[-10分] → C[-30分] → B[-1時間]）
--         favorites は created_at DESC（C[-1日] → B[-3日]）
--   ③ テスト店C（status='閉店疑い'・is_listed=false）も going/favorites に出る
--   ①6項目 store は store_id/name/category_l/category_s/area_label/gmaps_url のみ
-- ============================================================

BEGIN;

-- 冪等化：前回の本シードのみ掃除（テスト用マーカーで限定）
DELETE FROM going_list WHERE user_id IN (SELECT id FROM users WHERE google_sub = 'test-sub-mylist');
DELETE FROM favorites  WHERE user_id IN (SELECT id FROM users WHERE google_sub = 'test-sub-mylist');
DELETE FROM sessions   WHERE user_id IN (SELECT id FROM users WHERE google_sub = 'test-sub-mylist');
DELETE FROM stores     WHERE place_id IN ('test-place-A', 'test-place-B', 'test-place-C');
DELETE FROM users      WHERE google_sub = 'test-sub-mylist';

-- user
INSERT INTO users (google_sub, email)
VALUES ('test-sub-mylist', 'mylist-test@example.local');

-- session（既知トークンの SHA-256 hex、期限は未来30日）
INSERT INTO sessions (token_hash, user_id, expires_at)
SELECT 'c8f3acd15309dc1dcafb41f42c7c86a8a60731a535f5c21e4ec0579b0ff34874',
       u.id, now() + interval '30 days'
FROM users u WHERE u.google_sub = 'test-sub-mylist';

-- stores：A/B=営業中・掲載、C=閉店疑い・非掲載（③検証用）
INSERT INTO stores (name, category_l, category_s, address, lat, lng, place_id, gmaps_url,
                    area_label, status, confidence, curated_date, is_listed, updated_by)
VALUES
  ('テスト店A 営業中', '飲食', '居酒屋', '東京都練馬区豊玉北0-0-0', 35.7370, 139.6560,
   'test-place-A', 'https://maps.google.com/?q=test-A', '練馬', '営業中', '中', DATE '2026-07-22', true,  'import'),
  ('テスト店B 営業中', 'カフェ', '喫茶', '東京都練馬区豊玉上0-0-0', 35.7375, 139.6570,
   'test-place-B', 'https://maps.google.com/?q=test-B', '江古田', '営業中', '中', DATE '2026-07-22', true,  'import'),
  ('テスト店C 閉店疑い非掲載', '飲食', '焼肉', '東京都練馬区栄町0-0-0', 35.7380, 139.6580,
   'test-place-C', 'https://maps.google.com/?q=test-C', '江古田', '閉店疑い', '低', DATE '2026-07-22', false, 'ops');

-- going_list：
--   A を2タップ（-2時間 / -10分）→ DISTINCT ON (store_id) で -10分の1件に畳む（②）
--   B を1タップ（-1時間）、C を1タップ（-30分、閉店疑い・非掲載でも出る＝③）
INSERT INTO going_list (user_id, store_id, tapped_at, arrival_status)
SELECT u.id, s.id, v.ts, v.st
FROM (VALUES
        ('test-place-A', now() - interval '2 hours',    'none'),
        ('test-place-A', now() - interval '10 minutes', 'pending'),
        ('test-place-B', now() - interval '1 hour',     'none'),
        ('test-place-C', now() - interval '30 minutes', 'none')
     ) AS v(pid, ts, st)
JOIN stores s ON s.place_id = v.pid
CROSS JOIN users u
WHERE u.google_sub = 'test-sub-mylist';

-- favorites：2件（created_at 違い）。C（閉店疑い・非掲載）を新しい方に入れて③も兼ねる
INSERT INTO favorites (user_id, store_id, created_at)
SELECT u.id, s.id, v.ts
FROM (VALUES
        ('test-place-B', now() - interval '3 days'),
        ('test-place-C', now() - interval '1 day')
     ) AS v(pid, ts)
JOIN stores s ON s.place_id = v.pid
CROSS JOIN users u
WHERE u.google_sub = 'test-sub-mylist';

COMMIT;
