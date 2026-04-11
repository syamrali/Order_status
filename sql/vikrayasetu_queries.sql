-- =============================================================================
-- VIKRAYASETU + FARMER ENGAGEMENT — SQL REFERENCE (PostgreSQL)
-- =============================================================================
-- Purpose:
--   • Document every table column the app uses.
--   • Provide copy-paste queries you can run in psql / DBeaver to verify data.
--   • Mirror logic used in Python: backend/order_repository.py, farmer_engagement_db.py
-- Related: sql/maintenance_queries.sql — tagged [TAG-FE-*] / [TAG-VS-*] debug & fix flows
--
-- Environment mapping (see project root .env):
--   DATABASE_URL              → this database (vikrayasetu)
--   FARMER_ENGAGEMENT_DATABASE_URL → separate DB for master.users (phone → user_id)
--
-- Schema: transactions
-- =============================================================================


-- =============================================================================
-- SECTION A — TABLE: transactions.orders
-- =============================================================================
-- Used by: order_repository (main order row, filters, lookup by order reference)
-- Primary key (app default): order_id
-- Soft delete: is_deleted, is_active
-- =============================================================================

-- A.1) Full column list (as in your DDL)
-- -----------------------------------------------------------------------------
/*
    order_id
    reference_order_id
    internal_order_id
    order_number
    external_order_id
    checkout_id
    user_id
    subtotal
    discount
    shipping_method
    shipping_charge
    total_amount
    status
    payment_status
    payment_method
    cancelled_at
    cancelled_reason
    delivered_at
    is_test_order
    tags
    notes
    created_by
    updated_by
    created_at
    updated_at
    is_active
    is_deleted
    additional_info
    external_order_number
    parent_external_order_number
*/

-- A.2) Inspect active, non-deleted orders (safe default filter)
-- -----------------------------------------------------------------------------
SELECT
    o.order_id,
    o.order_number,
    o.external_order_number,
    o.internal_order_id,
    o.user_id,
    o.status,
    o.payment_status,
    o.payment_method,
    o.total_amount,
    o.delivered_at,
    o.created_at
FROM transactions.orders AS o
WHERE NOT COALESCE(o.is_deleted, false)
  AND COALESCE(o.is_active, true)
ORDER BY o.created_at DESC
LIMIT 100;


-- =============================================================================
-- SECTION B — TABLE: transactions.order_status_history
-- =============================================================================
-- Used by: order_repository — latest row = current “workflow” status (new_status)
-- Join: order_status_history.order_id = orders.order_id
-- App env: OSH_STATUS_COL=new_status, OSH_TIME_COL=changed_at
-- =============================================================================

-- B.1) Full column list
-- -----------------------------------------------------------------------------
/*
    history_id
    external_order_id
    order_id
    old_status
    new_status
    changed_at
    changed_by
    notes
    created_by
    updated_by
    created_at
    updated_at
    is_active
    is_deleted
*/

-- B.2) Latest status per order (one row per order — same idea as API subquery)
-- -----------------------------------------------------------------------------
SELECT DISTINCT ON (h.order_id)
    h.order_id,
    h.old_status,
    h.new_status,
    h.changed_at
FROM transactions.order_status_history AS h
WHERE NOT COALESCE(h.is_deleted, false)
ORDER BY h.order_id, h.changed_at DESC NULLS LAST;


-- =============================================================================
-- SECTION C — TABLE: transactions.order_items
-- =============================================================================
-- Used by: order_repository — json_agg line items (name, qty, price, …)
-- Join: order_items.order_id = orders.order_id
-- App env: ORDER_ITEMS_TABLE=order_items, OI_ORDER_BY_COL=order_item_id
-- =============================================================================

-- C.1) Full column list
-- -----------------------------------------------------------------------------
/*
    order_item_id
    order_id
    order_item_external_id
    product_variant_id
    quantity
    price
    total_amount
    shipping_partner
    created_by
    updated_by
    created_at
    updated_at
    is_active
    is_deleted
    external_order_number
*/

-- C.2) Line items for one order (replace :order_id)
-- -----------------------------------------------------------------------------
SELECT
    oi.order_item_id,
    oi.order_item_external_id,
    oi.product_variant_id,
    oi.quantity,
    oi.price,
    oi.total_amount
FROM transactions.order_items AS oi
WHERE oi.order_id = :order_id::uuid   -- adjust cast if your type is not uuid
  AND NOT COALESCE(oi.is_deleted, false)
ORDER BY oi.order_item_id;


-- =============================================================================
-- SECTION D — TABLE: transactions.order_payments
-- =============================================================================
-- Used by: optional reporting only (not yet merged into API JSON in Python)
-- Join: order_payments.order_id = orders.order_id
-- =============================================================================

-- D.1) Full column list
-- -----------------------------------------------------------------------------
/*
    order_payment_id
    order_id
    payment_method
    payment_status
    payment_amount
    payment_reference
    is_reverse_payment
    remarks
    payment_timestamp
    created_by
    updated_by
    created_at
    updated_at
    is_active
    is_deleted
*/

-- D.2) Payments for one order
-- -----------------------------------------------------------------------------
SELECT
    op.order_payment_id,
    op.payment_method,
    op.payment_status,
    op.payment_amount,
    op.payment_reference,
    op.payment_timestamp
FROM transactions.order_payments AS op
WHERE op.order_id = :order_id::uuid   -- adjust cast if needed
  AND NOT COALESCE(op.is_deleted, false)
ORDER BY op.payment_timestamp DESC NULLS LAST;


-- =============================================================================
-- SECTION E — TABLE: transactions.order_shipping_details
-- =============================================================================
-- Used by: optional reporting only (not yet merged into API JSON in Python)
-- Note: order_id in user schema is order_item_id here (per your DDL)
-- =============================================================================

-- E.1) Full column list
-- -----------------------------------------------------------------------------
/*
    order_shipping_id
    order_item_id
    shipping_method
    tracking_number
    tracking_url
    status
    created_by
    updated_by
    created_at
    updated_at
    is_active
    is_deleted
*/

-- E.2) Shipping rows linked to a line item (replace :order_item_id)
-- -----------------------------------------------------------------------------
SELECT
    os.shipping_method,
    os.tracking_number,
    os.tracking_url,
    os.status
FROM transactions.order_shipping_details AS os
WHERE os.order_item_id = :order_item_id
  AND NOT COALESCE(os.is_deleted, false);


-- =============================================================================
-- SECTION F — ORDER LOOKUP (same idea as API: user types any reference)
-- =============================================================================
-- App env ORDER_LOOKUP_COLUMNS lists columns to match (order_number, order_id, …)
-- Replace :lookup_value with the string the user typed.
-- =============================================================================

SELECT
    o.order_id,
    o.order_number,
    o.external_order_number,
    o.user_id,
    o.status
FROM transactions.orders AS o
WHERE NOT COALESCE(o.is_deleted, false)
  AND COALESCE(o.is_active, true)
  AND (
    -- Match any of these (same OR logic as in order_repository.py)
       o.order_number::text = :lookup_value
    OR o.order_id::text = :lookup_value
    OR o.external_order_number::text = :lookup_value
    OR o.internal_order_id::text = :lookup_value
    OR o.external_order_id::text = :lookup_value
    OR o.reference_order_id::text = :lookup_value
  )
LIMIT 1;


-- =============================================================================
-- SECTION G — LIST ORDERS FOR A USER (same idea as API after user_id is known)
-- =============================================================================
-- user_id comes from orders.user_id, often resolved from farmer engagement (mobile)
-- Replace :app_user_id
-- =============================================================================

SELECT
    COALESCE(
        NULLIF(TRIM(o.order_number::text), ''),
        NULLIF(TRIM(o.external_order_number::text), ''),
        o.order_id::text
    ) AS display_order_id,
    o.order_id,
    o.order_number,
    o.user_id,
    o.status,
    o.payment_status,
    o.total_amount,
    o.delivered_at
FROM transactions.orders AS o
WHERE NOT COALESCE(o.is_deleted, false)
  AND COALESCE(o.is_active, true)
  AND o.user_id::text = :app_user_id
ORDER BY o.created_at DESC;


-- =============================================================================
-- SECTION H — MASTER.USERS (farmer engagement DB — often a different database)
-- =============================================================================
-- Used by: backend/farmer_engagement_db.py
-- Maps: normalized 10-digit mobile → user_id (then transactions.orders.user_id)
-- App env: FE_USER_TABLE=master.users, FE_PHONE_COLUMN=phone, FE_USER_ID_COLUMN=user_id
-- Run against the connection in FARMER_ENGAGEMENT_DATABASE_URL (not vikrayasetu if split)
-- =============================================================================

-- H.0) Full column list (your DDL)
-- -----------------------------------------------------------------------------
/*
    user_id
    user_external_id
    email
    country_code
    phone
    first_name
    last_name
    language_code
    profile_pic
    is_verified
    tags
    user_type
    location
    device_id
    fcm_token
    creation_type
    current_version
    last_app_logged_in
    last_web_logged_in
    created_by
    updated_by
    created_at
    updated_at
    is_active
    is_deleted
    additional_info
    guest_user_id
    token_version
*/

-- H.1) Resolve user_id from mobile — SAME logic as Python (phone column, last 10 digits)
-- -----------------------------------------------------------------------------
-- Replace :d10 with '9876543210' style string (no +91 required; digits are normalized)
SELECT u.user_id AS resolved_uid
FROM master.users AS u
WHERE RIGHT(
    REGEXP_REPLACE(COALESCE(u.phone::text, ''), '[^0-9]', '', 'g'),
    10
) = :d10
  AND NOT COALESCE(u.is_deleted, false)
  AND COALESCE(u.is_active, true)
LIMIT 2;

-- H.2) Optional: preview user row after you know user_id
-- -----------------------------------------------------------------------------
/*
SELECT
    u.user_id,
    u.user_external_id,
    u.email,
    u.country_code,
    u.phone,
    u.first_name,
    u.last_name,
    u.is_verified,
    u.is_active,
    u.is_deleted
FROM master.users AS u
WHERE u.user_id = :user_id
LIMIT 1;
*/

-- H.3) If you store E.164 in phone and need country_code + phone, extend the WHERE with
--      REGEXP_REPLACE on CONCAT(u.country_code, u.phone) — keep Python + SQL in sync.


-- =============================================================================
-- SECTION I — END-TO-END CHECK (vikrayasetu only — one order with lines + history)
-- =============================================================================
-- Replace :order_id with a real orders.order_id
-- =============================================================================

-- I.1) Header row
SELECT o.*
FROM transactions.orders AS o
WHERE o.order_id = :order_id::uuid   -- adjust type if not uuid
  AND NOT COALESCE(o.is_deleted, false);

-- I.2) Status history
SELECT h.*
FROM transactions.order_status_history AS h
WHERE h.order_id = :order_id::uuid
ORDER BY h.changed_at DESC;

-- I.3) Items
SELECT oi.*
FROM transactions.order_items AS oi
WHERE oi.order_id = :order_id::uuid
ORDER BY oi.order_item_id;

-- I.4) Payments
SELECT op.*
FROM transactions.order_payments AS op
WHERE op.order_id = :order_id::uuid
ORDER BY op.payment_timestamp DESC;


-- =============================================================================
-- NOTES
-- =============================================================================
-- • Cast :order_id::uuid only if your column type is uuid; use ::bigint etc. if not.
-- • This file is documentation only; the Python app builds SQL dynamically from .env.
-- • Keep in sync with: backend/order_schema_config.py, backend/order_repository.py,
--   backend/farmer_engagement_db.py (master.users)
-- =============================================================================
