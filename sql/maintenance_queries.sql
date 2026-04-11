-- =============================================================================
--  ORDER STATUS ASSISTANT — MAINTENANCE & DEBUG QUERIES (PostgreSQL)
-- =============================================================================
--
--  WHAT THIS FILE IS FOR
--  ----------------------
--  • Run these in psql, DBeaver, or any SQL client when something “doesn’t match”
--    the app (no orders, wrong user, phone not resolving).
--  • Every block is labeled so you can search this file by section number or tag.
--
--  RELATED FILE (full table docs + copy-paste reference SQL)
--  ----------------------------------------------------------
--  • sql/vikrayasetu_queries.sql  — column lists, A–I sections, end-to-end samples
--
--  PYTHON FILES THIS SHOULD STAY ALIGNED WITH
--  ------------------------------------------
--  • backend/farmer_engagement_db.py   — master.users → user_id (phone)
--  • backend/order_repository.py        — transactions.orders + history + items
--  • backend/order_schema_config.py     — .env → table/column names
--
--  TWO DATABASES (often two different connection strings)
--  -------------------------------------------------------
--  [FE]  Farmer engagement  →  master.users  (FARMER_ENGAGEMENT_DATABASE_URL)
--  [VS]  Vikrayasetu        →  transactions.*  (DATABASE_URL)
--
-- =============================================================================


-- =============================================================================
--  INDEX — jump by searching for the tag in your editor (e.g. [TAG-FE-01])
-- =============================================================================
--
--  [TAG-FE-00]  Which database am I connected to?  (sanity check)
--  [TAG-FE-01]  master.users — list columns (information_schema)
--  [TAG-FE-02]  master.users — same phone resolution as Python (10-digit tail)
--  [TAG-FE-03]  master.users — optional: country_code + phone (if you use split storage)
--  [TAG-FE-04]  master.users — find duplicate / ambiguous phones (same last 10 digits)
--  [TAG-FE-05]  master.users — soft-delete filter ON vs OFF (compare row counts)
--
--  [TAG-VS-01]  transactions.orders — does user_id exist? (after FE resolution)
--  [TAG-VS-02]  transactions.orders — quick profile: how many orders per user_id
--  [TAG-VS-03]  Type check — is orders.order_id uuid or something else? (adjust casts)
--
--  [TAG-E2E-01]  Manual pipeline: phone → user_id → orders (two connections)
--
--  [TAG-FIX-01]  Troubleshooting checklist (comments only — read before running)
--
-- =============================================================================


-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
--  [TAG-FE-00]  WHICH DATABASE IS THIS?  (run first if unsure)
-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
--  PURPOSE: Confirm you are on farmer engagement vs vikrayasetu before debugging.
--  NOTE:    Same SQL works on both; result tells you which catalog you hit.
-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::

SELECT current_database() AS db_name, current_user AS db_user, now() AS server_time;


-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
--  [TAG-FE-01]  LIST COLUMNS — master.users  (farmer engagement DB)
-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
--  PURPOSE: Verify column names match .env: FE_PHONE_COLUMN, FE_USER_ID_COLUMN
--  EXPECT:   You should see at least: user_id, phone, is_active, is_deleted
-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::

SELECT
    column_name,
    data_type,
    is_nullable
FROM information_schema.columns
WHERE table_schema = 'master'
  AND table_name = 'users'
ORDER BY ordinal_position;


-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
--  [TAG-FE-02]  RESOLVE user_id FROM MOBILE — matches backend/farmer_engagement_db.py
-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
--  PURPOSE: Same logic as Python: strip non-digits, take RIGHT 10, match phone.
--  REPLACE:  Change '9876543210' to the 10 digits the user typed (no +91 needed).
--  ENV:      FE_USER_TABLE=master.users  FE_PHONE_COLUMN=phone  FE_USER_ID_COLUMN=user_id
--  FILTER:   FE_FILTER_SOFT_DELETE=true → NOT is_deleted AND is_active
-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::

SELECT
    u.user_id AS resolved_uid,
    u.phone AS raw_phone,
    RIGHT(REGEXP_REPLACE(COALESCE(u.phone::text, ''), '[^0-9]', '', 'g'), 10) AS digits_last_10,
    u.is_active,
    u.is_deleted
FROM master.users AS u
WHERE RIGHT(REGEXP_REPLACE(COALESCE(u.phone::text, ''), '[^0-9]', '', 'g'), 10) = '9876543210'
  AND NOT COALESCE(u.is_deleted, false)
  AND COALESCE(u.is_active, true)
LIMIT 5;


-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
--  [TAG-FE-03]  OPTIONAL — country_code + phone  (only if data is stored split)
-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
--  PURPOSE: If `phone` does NOT include country code but `country_code` does,
--           normalize CONCAT(country_code, phone) the same way as [TAG-FE-02].
--  ACTION:  If you adopt this in production, update farmer_engagement_db.py too.
-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::

/*
SELECT u.user_id AS resolved_uid,
       u.country_code,
       u.phone,
       RIGHT(
           REGEXP_REPLACE(
               COALESCE(u.country_code::text, '') || COALESCE(u.phone::text, ''),
               '[^0-9]', '', 'g'
           ),
           10
       ) AS digits_last_10
FROM master.users AS u
WHERE RIGHT(
          REGEXP_REPLACE(
              COALESCE(u.country_code::text, '') || COALESCE(u.phone::text, ''),
              '[^0-9]', '', 'g'
          ),
          10
      ) = '9876543210'
  AND NOT COALESCE(u.is_deleted, false)
  AND COALESCE(u.is_active, true)
LIMIT 5;
*/


-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
--  [TAG-FE-04]  FIND AMBIGUOUS PHONES — more than one user_id for same last-10
-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
--  PURPOSE: Python returns the first row if multiple match; you should know if
--           your data has collisions.
-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::

SELECT
    RIGHT(REGEXP_REPLACE(COALESCE(u.phone::text, ''), '[^0-9]', '', 'g'), 10) AS last_10,
    COUNT(*) AS user_rows,
    ARRAY_AGG(u.user_id::text) AS user_ids
FROM master.users AS u
WHERE NOT COALESCE(u.is_deleted, false)
  AND COALESCE(u.is_active, true)
GROUP BY 1
HAVING COUNT(*) > 1
ORDER BY COUNT(*) DESC
LIMIT 50;


-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
--  [TAG-FE-05]  COMPARE — with vs without soft-delete filter (row counts)
-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
--  PURPOSE: If FE_FILTER_SOFT_DELETE=false in .env, use the “no filter” query.
-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::

-- With filter (default app behavior)
SELECT COUNT(*) AS cnt_with_soft_delete_rules
FROM master.users AS u
WHERE RIGHT(REGEXP_REPLACE(COALESCE(u.phone::text, ''), '[^0-9]', '', 'g'), 10) = '9876543210'
  AND NOT COALESCE(u.is_deleted, false)
  AND COALESCE(u.is_active, true);

-- Without filter (debug only — includes deleted/inactive)
SELECT COUNT(*) AS cnt_no_soft_delete_rules
FROM master.users AS u
WHERE RIGHT(REGEXP_REPLACE(COALESCE(u.phone::text, ''), '[^0-9]', '', 'g'), 10) = '9876543210';


-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
--  [TAG-VS-01]  VIKRAYASETU — orders for a resolved user_id
-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
--  PURPOSE: After [TAG-FE-02] returns user_id, run this on DATABASE_URL (vikrayasetu).
--  REPLACE:  'PUT-USER-ID-HERE' with the uuid/text your DB uses for user_id.
--  NOTE:     If user_id is uuid, cast explicitly; if text/bigint, remove ::uuid.
-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::

SELECT
    o.order_id,
    o.order_number,
    o.user_id,
    o.status,
    o.payment_status,
    o.total_amount,
    o.created_at
FROM transactions.orders AS o
WHERE NOT COALESCE(o.is_deleted, false)
  AND COALESCE(o.is_active, true)
  AND o.user_id::text = 'PUT-USER-ID-HERE'
ORDER BY o.created_at DESC
LIMIT 50;


-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
--  [TAG-VS-02]  VIKRAYASETU — how many orders per user_id (spot-check scale)
-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
--  PURPOSE: Quick histogram; use after joining FE user_id into orders.
-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::

SELECT
    o.user_id::text AS user_id,
    COUNT(*) AS order_count
FROM transactions.orders AS o
WHERE NOT COALESCE(o.is_deleted, false)
  AND COALESCE(o.is_active, true)
GROUP BY o.user_id
ORDER BY COUNT(*) DESC
LIMIT 30;


-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
--  [TAG-VS-03]  TYPE CHECK — orders.order_id and orders.user_id data types
-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
--  PURPOSE: sql/vikrayasetu_queries.sql uses :order_id::uuid in places; if your
--           PK is bigint, change casts in manual queries to ::bigint.
-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::

SELECT
    c.column_name,
    c.data_type,
    c.udt_name
FROM information_schema.columns AS c
WHERE c.table_schema = 'transactions'
  AND c.table_name = 'orders'
  AND c.column_name IN ('order_id', 'user_id', 'order_number')
ORDER BY c.column_name;


-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
--  [TAG-E2E-01]  MANUAL END-TO-END (two steps, two connections if DBs are split)
-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
--  STEP 1 — On FARMER_ENGAGEMENT_DATABASE_URL:
--           Run [TAG-FE-02] with the customer’s 10-digit number → copy user_id
--
--  STEP 2 — On DATABASE_URL (vikrayasetu):
--           Run [TAG-VS-01] with that user_id → you should see their orders
--
--  If STEP 1 returns 0 rows:  phone mismatch, wrong DB, or FE_FILTER_SOFT_DELETE
--  If STEP 2 returns 0 rows:   orders live under another user_id or different env
-- :::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::


-- =============================================================================
--  [TAG-FIX-01]  TROUBLESHOOTING CHECKLIST (read — do not execute)
-- =============================================================================
--
--  Symptom: API says farmer_engagement_db_configured: false
--  Fix:     Set FARMER_ENGAGEMENT_DATABASE_URL in .env and restart the API.
--
--  Symptom: Phone resolves in SQL [TAG-FE-02] but app does not resolve user_id
--  Fix:     Confirm API uses same .env file; check FE_USER_TABLE / FE_PHONE_COLUMN.
--
--  Symptom: user_id resolves but zero orders
--  Fix:     Run [TAG-VS-01] with that id; check ORDERS_USER_ID_COL and soft-delete
--           flags ORDERS_APPLY_SOFT_DELETE_FILTER in .env.
--
--  Symptom: “duplicate” users for one mobile
--  Fix:     Run [TAG-FE-04]; clean data or tighten business rules.
--
--  Symptom: SQL errors on ::uuid casts
--  Fix:     Run [TAG-VS-03]; align types with sql/vikrayasetu_queries.sql section I.
--
-- =============================================================================
--  END OF FILE
-- =============================================================================
