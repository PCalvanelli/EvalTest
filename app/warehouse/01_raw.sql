-- Raw layer: source tables as an EL tool (Fivetran-style) would land them. Untransformed:
-- source-native column names, string/cents/epoch amounts and timestamps, soft deletes, test rows.
-- All data is deterministic: h(x, n) is a pseudo-random integer in [0, n).

CREATE TEMP MACRO h(x, n) AS CAST(hash(x) % n AS INTEGER);

CREATE SCHEMA raw_shopify;
CREATE SCHEMA raw_stripe;
CREATE SCHEMA raw_zendesk;
CREATE SCHEMA raw_hubspot;
CREATE SCHEMA raw_ads;
CREATE SCHEMA raw_erp;
CREATE SCHEMA raw_finance;

-- ───────────────────────────── raw_finance ─────────────────────────────

CREATE TABLE raw_finance.fx_rates AS
SELECT
    CAST(d AS DATE) AS date,
    cur AS currency,
    CASE cur
        WHEN 'USD' THEN 1.0
        WHEN 'EUR' THEN round(1.08 + (h(datediff('day', DATE '2023-01-01', d) * 7, 600) - 300) / 10000.0, 4)
        ELSE round(1.27 + (h(datediff('day', DATE '2023-01-01', d) * 11, 600) - 300) / 10000.0, 4)
    END AS rate_to_usd,
    TIMESTAMP '2025-10-01 02:00:00' AS _loaded_at
FROM generate_series(TIMESTAMP '2023-12-01', TIMESTAMP '2025-09-30', INTERVAL 1 DAY) g(d)
CROSS JOIN (VALUES ('USD'), ('EUR'), ('GBP')) c(cur);
COMMENT ON TABLE raw_finance.fx_rates IS 'Daily FX rates loaded from the treasury provider. One row per date and currency.';
COMMENT ON COLUMN raw_finance.fx_rates.rate_to_usd IS 'Multiply an amount in `currency` by this to get USD.';

-- ───────────────────────────── raw_shopify ─────────────────────────────

CREATE TABLE raw_shopify.customers AS
SELECT
    5000 + i AS id,
    'customer' || i || '@example.com' AS email,
    ['Ava', 'Ben', 'Chloe', 'Dev', 'Elena', 'Finn', 'Grace', 'Hugo', 'Isla', 'Jae', 'Kofi', 'Lena', 'Mateo', 'Nora', 'Omar', 'Priya'][h(i * 3, 16) + 1] AS first_name,
    ['Smith', 'Garcia', 'Chen', 'Patel', 'Müller', 'Okafor', 'Rossi', 'Kim', 'Dubois', 'Silva'][h(i * 5, 10) + 1] AS last_name,
    ['US', 'US', 'US', 'US', 'UK', 'DE', 'FR', 'CA', 'NL', 'AU'][h(i * 7, 10) + 1] AS country_code,
    TIMESTAMP '2023-01-01' + to_seconds(CAST(h(i * 11, 500) AS BIGINT) * 86400 + h(i * 13, 86400)) AS created_at,
    h(i * 17, 3) > 0 AS accepts_marketing,
    CASE WHEN h(i * 19, 25) = 0 THEN 'wholesale, net30' WHEN h(i * 19, 25) = 1 THEN 'vip' ELSE '' END AS tags,
    h(i * 23, 50) = 0 AS _fivetran_deleted,
    TIMESTAMP '2025-10-01 02:00:00' AS _fivetran_synced
FROM range(1, 401) t(i);
COMMENT ON TABLE raw_shopify.customers IS 'Shopify customers (Fivetran sync). Includes soft-deleted rows (_fivetran_deleted).';
COMMENT ON COLUMN raw_shopify.customers.tags IS 'Comma-separated Shopify tags, e.g. "wholesale, net30" or "vip".';
COMMENT ON COLUMN raw_shopify.customers._fivetran_deleted IS 'TRUE when the record was deleted in Shopify.';

CREATE TABLE raw_shopify.products AS
WITH p AS (
    SELECT i, ['Footwear', 'Apparel', 'Accessories', 'Bags', 'Gear'][(i - 1) % 5 + 1] AS cat, (i - 1) // 5 + 1 AS k
    FROM range(1, 41) t(i)
)
SELECT
    100 + i AS id,
    ['Summit', 'Coastal', 'Urban', 'Alpine', 'Trail', 'Harbor', 'Canyon', 'Nordic'][k] || ' ' || CASE cat
        WHEN 'Footwear' THEN ['Runner', 'Boot', 'Sneaker', 'Sandal', 'Trail Shoe', 'Clog', 'Slip-On', 'Trainer'][k]
        WHEN 'Apparel' THEN ['Tee', 'Hoodie', 'Rain Jacket', 'Fleece', 'Down Vest', 'Chino', 'Base Layer', 'Shell'][k]
        WHEN 'Accessories' THEN ['Cap', 'Beanie', 'Socks 3-Pack', 'Gloves', 'Belt', 'Bottle', 'Sunglasses', 'Scarf'][k]
        WHEN 'Bags' THEN ['Daypack', 'Duffel', 'Sling', 'Tote', 'Messenger', 'Hip Pack', 'Roller', 'Dry Bag'][k]
        ELSE ['Headlamp', 'Trekking Poles', 'Tent 2P', 'Sleeping Pad', 'Stove', 'Camp Chair', 'Lantern', 'Hammock'][k]
    END AS title,
    cat AS product_type,
    ['Northpeak', 'Bluewater', 'Northpeak', 'Fieldhouse'][h(i * 3, 4) + 1] AS vendor,
    upper(substr(cat, 1, 3)) || '-' || lpad(CAST(100 + i AS VARCHAR), 4, '0') AS sku,
    CASE WHEN i > 38 THEN 'archived' ELSE 'active' END AS status,
    TIMESTAMP '2023-06-01' + to_days(i) AS created_at,
    FALSE AS _fivetran_deleted
FROM p;
COMMENT ON TABLE raw_shopify.products IS 'Shopify products (Fivetran sync). Current price lives in product_price_changes.';
COMMENT ON COLUMN raw_shopify.products.product_type IS 'Merchandising category.';

CREATE TABLE raw_shopify.product_price_changes AS
WITH base AS (SELECT i, 100 + i AS product_id, 15 + h(i * 7, 160) + 0.99 AS base_price FROM range(1, 41) t(i))
SELECT
    product_id,
    CAST(round(base_price * (1 + 0.08 * k), 2) AS DECIMAL(10, 2)) AS price,
    CASE k
        WHEN 0 THEN TIMESTAMP '2023-12-01'
        WHEN 1 THEN TIMESTAMP '2024-08-01' + to_days(h(i * 3, 60))
        ELSE TIMESTAMP '2025-03-01' + to_days(h(i * 5, 60))
    END AS changed_at
FROM base CROSS JOIN range(0, 3) r(k)
WHERE k = 0 OR (k = 1 AND h(i, 2) = 0) OR (k = 2 AND h(i, 2) = 0 AND h(i * 11, 2) = 0)
ORDER BY product_id, changed_at;
COMMENT ON TABLE raw_shopify.product_price_changes IS 'Audit log of list-price changes in USD, one row per change.';

CREATE TEMP TABLE _orders AS
SELECT
    i,
    9000 + i AS id,
    5000 + h(i * 3, 380) + 1 AS customer_id,
    TIMESTAMP '2024-01-01' + to_seconds(CAST(h(i * 13, 639) AS BIGINT) * 86400 + h(i * 29, 86400)) AS created_at,
    ['USD', 'USD', 'USD', 'USD', 'EUR', 'EUR', 'GBP'][h(i * 31, 7) + 1] AS currency,
    h(i * 37, 100) AS r_status,
    h(i * 41, 200) < 3 AS test,
    ['web', 'web', 'web', 'pos', 'mobile_app'][h(i * 43, 5) + 1] AS source_name,
    h(i * 47, 100) = 0 AS _fivetran_deleted
FROM range(1, 2501) t(i);

CREATE TEMP TABLE _lines AS
SELECT
    o.id * 10 + k AS id,
    o.id AS order_id,
    o.created_at,
    o.currency,
    101 + h(o.i * 53 + k, 38) AS product_id,  -- products 139-140 are never ordered
    h(o.i * 59 + k, 3) + 1 AS quantity,
    CASE WHEN h(o.i * 67 + k, 10) = 0 THEN 0.1 ELSE 0 END AS discount_pct
FROM _orders o CROSS JOIN range(0, 3) r(k)
WHERE k < h(o.i * 61, 3) + 1;

CREATE TABLE raw_shopify.order_lines AS
SELECT
    l.id,
    l.order_id,
    l.product_id,
    l.quantity,
    CAST(round(p.price / fx.rate_to_usd, 2) AS DECIMAL(10, 2)) AS price,
    CAST(round(l.quantity * round(p.price / fx.rate_to_usd, 2) * l.discount_pct, 2) AS DECIMAL(10, 2)) AS total_discount
FROM _lines l
ASOF JOIN raw_shopify.product_price_changes p ON p.product_id = l.product_id AND l.created_at >= p.changed_at
JOIN raw_finance.fx_rates fx ON fx.currency = l.currency AND fx.date = CAST(l.created_at AS DATE)
ORDER BY l.id;
COMMENT ON TABLE raw_shopify.order_lines IS 'Shopify order line items. Prices are in the order currency.';
COMMENT ON COLUMN raw_shopify.order_lines.price IS 'Unit price in the order currency, before discount.';
COMMENT ON COLUMN raw_shopify.order_lines.total_discount IS 'Discount applied to the whole line, in the order currency.';

CREATE TABLE raw_shopify.orders AS
WITH totals AS (
    SELECT order_id, SUM(quantity * price - total_discount) AS subtotal FROM raw_shopify.order_lines GROUP BY 1
), amounts AS (
    SELECT o.*, t.subtotal,
           CASE WHEN t.subtotal >= 100 THEN 0 ELSE 7.95 END AS shipping,
           round(t.subtotal * 0.08, 2) AS tax
    FROM _orders o JOIN totals t ON t.order_id = o.id
)
SELECT
    id,
    '#' || (1000 + i) AS name,
    customer_id,
    created_at,
    currency,
    CAST(subtotal AS VARCHAR) AS subtotal_price,
    CAST(shipping AS VARCHAR) AS total_shipping_price,
    CAST(tax AS VARCHAR) AS total_tax,
    CAST(subtotal + shipping + tax AS VARCHAR) AS total_price,
    CASE
        WHEN r_status < 82 THEN 'paid'
        WHEN r_status < 86 THEN 'refunded'
        WHEN r_status < 90 THEN 'partially_refunded'
        WHEN r_status < 93 THEN 'pending'
        ELSE 'voided'
    END AS financial_status,
    CASE WHEN r_status >= 93 THEN created_at + INTERVAL 2 HOUR END AS cancelled_at,
    test,
    source_name,
    _fivetran_deleted,
    TIMESTAMP '2025-10-01 02:00:00' AS _fivetran_synced
FROM amounts
ORDER BY id;
COMMENT ON TABLE raw_shopify.orders IS 'Shopify orders (Fivetran sync). Money columns are strings in the order currency. Includes test orders and soft-deleted rows.';
COMMENT ON COLUMN raw_shopify.orders.subtotal_price IS 'String. Sum of line prices minus discounts, order currency.';
COMMENT ON COLUMN raw_shopify.orders.total_price IS 'String. Subtotal + shipping + tax, order currency.';
COMMENT ON COLUMN raw_shopify.orders.financial_status IS 'paid, pending, refunded, partially_refunded, voided.';
COMMENT ON COLUMN raw_shopify.orders.test IS 'TRUE for test orders placed by staff.';
COMMENT ON COLUMN raw_shopify.orders.created_at IS 'UTC.';

CREATE TABLE raw_shopify.refunds AS
SELECT
    300000 + id AS id,
    id AS order_id,
    created_at + to_days(3 + h(id * 71, 20)) AS created_at,
    CAST(CASE financial_status
        WHEN 'refunded' THEN CAST(total_price AS DECIMAL(12, 2))
        ELSE round(CAST(subtotal_price AS DECIMAL(12, 2)) * 0.3, 2)
    END AS DECIMAL(12, 2)) AS amount,
    currency
FROM raw_shopify.orders
WHERE financial_status IN ('refunded', 'partially_refunded')
ORDER BY id;
COMMENT ON TABLE raw_shopify.refunds IS 'Shopify refunds. Amount is in the order currency and can include shipping and tax.';

-- ───────────────────────────── raw_stripe ─────────────────────────────

CREATE TABLE raw_stripe.plans AS
SELECT * FROM (VALUES
    ('price_basic_monthly', 'Basic Monthly', 1900, 'month', 'usd', TRUE),
    ('price_pro_monthly', 'Pro Monthly', 4900, 'month', 'usd', TRUE),
    ('price_basic_annual', 'Basic Annual', 19000, 'year', 'usd', TRUE),
    ('price_pro_annual', 'Pro Annual', 49000, 'year', 'usd', TRUE)
) v(id, nickname, amount, "interval", currency, active);
COMMENT ON TABLE raw_stripe.plans IS 'Stripe prices for the membership program. amount is in cents.';
COMMENT ON COLUMN raw_stripe.plans.amount IS 'Integer cents per billing interval.';

CREATE TABLE raw_stripe.customers AS
SELECT
    'cus_' || lpad(CAST(i AS VARCHAR), 6, '0') AS id,
    'customer' || (i * 2) || '@example.com' AS email,
    CAST(epoch(TIMESTAMP '2024-01-01' + to_days(h(i * 3, 600))) AS BIGINT) AS created,
    'usd' AS currency
FROM range(1, 181) t(i);
COMMENT ON TABLE raw_stripe.customers IS 'Stripe customers. created is a Unix epoch (seconds).';

CREATE TEMP TABLE _subs AS
SELECT *, CASE WHEN will_cancel AND start_date + lifetime_days <= DATE '2025-09-30' THEN start_date + lifetime_days END AS cancel_date
FROM (
    SELECT
        i,
        'sub_' || lpad(CAST(i AS VARCHAR), 6, '0') AS id,
        'cus_' || lpad(CAST(h(i * 3, 180) + 1 AS VARCHAR), 6, '0') AS customer,
        ['price_basic_monthly', 'price_basic_monthly', 'price_pro_monthly', 'price_pro_monthly', 'price_basic_annual', 'price_pro_annual'][h(i * 5, 6) + 1] AS plan_id,
        DATE '2024-01-01' + h(i * 7, 600) AS start_date,
        h(i * 11, 100) < 35 AS will_cancel,
        60 + h(i * 13, 340) AS lifetime_days
    FROM range(1, 221) t(i)
);

CREATE TABLE raw_stripe.subscriptions AS
SELECT
    id,
    customer,
    plan_id AS plan,
    CASE WHEN cancel_date IS NOT NULL THEN 'canceled' WHEN h(i * 17, 20) = 0 THEN 'past_due' ELSE 'active' END AS status,
    CAST(epoch(CAST(start_date AS TIMESTAMP)) AS BIGINT) AS start_date,
    CAST(epoch(CAST(cancel_date AS TIMESTAMP)) AS BIGINT) AS canceled_at,
    CAST(epoch(CAST(start_date AS TIMESTAMP)) AS BIGINT) AS created
FROM _subs ORDER BY id;
COMMENT ON TABLE raw_stripe.subscriptions IS 'Stripe subscriptions. Timestamps are Unix epochs (seconds). plan references raw_stripe.plans.id.';
COMMENT ON COLUMN raw_stripe.subscriptions.status IS 'active, past_due, canceled.';

CREATE TABLE raw_stripe.invoices AS
WITH periods AS (
    SELECT s.i, s.id AS subscription, s.customer, s.cancel_date, p.amount, k,
           CAST(s.start_date + to_months(CASE WHEN p."interval" = 'year' THEN 12 * k ELSE k END) AS DATE) AS period_start
    FROM _subs s JOIN raw_stripe.plans p ON p.id = s.plan_id CROSS JOIN range(0, 22) r(k)
)
SELECT
    'in_' || lpad(CAST(i AS VARCHAR), 6, '0') || '_' || lpad(CAST(k AS VARCHAR), 2, '0') AS id,
    subscription,
    customer,
    amount AS amount_due,
    CASE WHEN h(i * 19 + k, 25) = 0 THEN 0 ELSE amount END AS amount_paid,
    CASE WHEN h(i * 19 + k, 25) = 0 THEN 'uncollectible' ELSE 'paid' END AS status,
    CAST(epoch(CAST(period_start AS TIMESTAMP)) AS BIGINT) AS period_start,
    CAST(epoch(CAST(period_start AS TIMESTAMP)) AS BIGINT) + 3600 AS created,
    'usd' AS currency
FROM periods
WHERE period_start <= DATE '2025-09-30' AND (cancel_date IS NULL OR period_start < cancel_date)
ORDER BY id;
COMMENT ON TABLE raw_stripe.invoices IS 'Stripe invoices, one per subscription billing period. Amounts in cents, timestamps are Unix epochs.';

CREATE TABLE raw_stripe.charges AS
SELECT
    'ch_' || substr(id, 4) AS id,
    id AS invoice,
    customer,
    amount_paid AS amount,
    'succeeded' AS status,
    created + 60 AS created,
    currency
FROM raw_stripe.invoices WHERE status = 'paid';
COMMENT ON TABLE raw_stripe.charges IS 'Stripe card charges for paid invoices. Amount in cents.';

-- ───────────────────────────── raw_zendesk ─────────────────────────────

CREATE TABLE raw_zendesk.users AS
SELECT 70000 + i AS id, 'customer' || (i + 20) || '@example.com' AS email,
       'Customer ' || (i + 20) AS name, 'end-user' AS role,
       TIMESTAMP '2023-06-01' + to_days(h(i * 3, 700)) AS created_at
FROM range(1, 261) t(i)
UNION ALL
SELECT 80000 + i, 'agent' || i || '@support.example.com', 'Agent ' || i, 'agent', TIMESTAMP '2023-01-15'
FROM range(1, 21) t(i);
COMMENT ON TABLE raw_zendesk.users IS 'Zendesk users: customers (role = end-user) and support staff (role = agent).';

CREATE TEMP TABLE _tickets AS
SELECT *,
    CASE WHEN r_status < 70 THEN 'closed' WHEN r_status < 85 THEN 'solved' WHEN r_status < 92 THEN 'open'
         WHEN r_status < 97 THEN 'pending' ELSE 'new' END AS status
FROM (
    SELECT
        i,
        400000 + i AS id,
        70000 + h(i * 3, 260) + 1 AS requester_id,
        80000 + h(i * 5, 20) + 1 AS assignee_id,
        TIMESTAMP '2024-01-01' + to_seconds(CAST(h(i * 7, 639) AS BIGINT) * 86400 + h(i * 11, 86400)) AS created_at,
        [NULL, 'low', 'normal', 'normal', 'normal', 'high', 'urgent'][h(i * 13, 7) + 1] AS priority,
        ['email', 'web', 'chat', 'phone'][h(i * 17, 4) + 1] AS via_channel,
        ['Where is my order?', 'Refund request', 'Sizing question', 'Damaged item', 'Subscription billing', 'Change shipping address'][h(i * 19, 6) + 1] AS subject,
        h(i * 23, 100) AS r_status,
        i AS seed
    FROM range(1, 901) t(i)
);
ALTER TABLE _tickets ADD COLUMN first_reply_hours INTEGER;
UPDATE _tickets SET first_reply_hours = CASE WHEN priority = 'urgent' THEN 1 + h(seed * 29, 6) ELSE 1 + h(seed * 29, 48) END;

CREATE TABLE raw_zendesk.tickets AS
SELECT
    id, requester_id, assignee_id, subject, status, priority, via_channel,
    CASE WHEN h(i * 31, 5) = 0 THEN 'vip' ELSE '' END AS tags,
    created_at,
    CASE WHEN status IN ('solved', 'closed') THEN created_at + to_hours(first_reply_hours + 4 + h(i * 37, 200)) END AS solved_at,
    created_at + to_hours(first_reply_hours + 4 + h(i * 37, 200) + 24) AS updated_at
FROM _tickets ORDER BY id;
COMMENT ON TABLE raw_zendesk.tickets IS 'Zendesk support tickets. priority can be NULL (not set). Timestamps UTC.';
COMMENT ON COLUMN raw_zendesk.tickets.status IS 'new, open, pending, solved, closed.';
COMMENT ON COLUMN raw_zendesk.tickets.priority IS 'low, normal, high, urgent, or NULL when unset.';

CREATE TABLE raw_zendesk.ticket_comments AS
SELECT
    5000000 + t.i * 10 + k AS id,
    t.id AS ticket_id,
    CASE WHEN k = 0 THEN t.requester_id ELSE t.assignee_id END AS author_id,
    k <> 1 AS public,
    CASE k
        WHEN 0 THEN t.created_at
        WHEN 1 THEN t.created_at + INTERVAL 20 MINUTE
        WHEN 2 THEN t.created_at + to_hours(t.first_reply_hours)
        ELSE t.created_at + to_hours(t.first_reply_hours + 6)
    END AS created_at
FROM _tickets t CROSS JOIN range(0, 4) r(k)
WHERE k = 0 OR (t.status <> 'new' AND (k = 2 OR (k = 1 AND h(t.i * 41, 3) = 0) OR (k = 3 AND h(t.i * 43, 2) = 0)))
ORDER BY id;
COMMENT ON TABLE raw_zendesk.ticket_comments IS 'Zendesk ticket comments. public = FALSE marks internal notes that the customer never sees.';

-- ───────────────────────────── raw_hubspot ─────────────────────────────

CREATE TABLE raw_hubspot.contacts AS
SELECT
    1000 + i AS vid,
    'customer' || i || '@example.com' AS property_email,
    CASE WHEN i <= 400 THEN 'customer'
         ELSE ['subscriber', 'lead', 'marketingqualifiedlead', 'salesqualifiedlead', 'opportunity'][h(i * 3, 5) + 1] END AS property_lifecyclestage,
    ['ORGANIC_SEARCH', 'PAID_SEARCH', 'PAID_SOCIAL', 'EMAIL_MARKETING', 'DIRECT_TRAFFIC', 'REFERRALS'][h(i * 5, 6) + 1] AS property_hs_analytics_source,
    CAST(epoch(TIMESTAMP '2022-10-01' + to_days(h(i * 7, 1000))) AS BIGINT) * 1000 AS property_createdate,
    FALSE AS _fivetran_deleted
FROM range(1, 601) t(i);
COMMENT ON TABLE raw_hubspot.contacts IS 'HubSpot CRM contacts, both leads and customers. property_createdate is epoch milliseconds.';
COMMENT ON COLUMN raw_hubspot.contacts.property_hs_analytics_source IS 'Original acquisition source, e.g. PAID_SEARCH, ORGANIC_SEARCH.';

CREATE TABLE raw_hubspot.deals AS
SELECT
    900000 + i AS deal_id,
    'Wholesale - ' || ['Q1', 'Q2', 'Q3', 'Q4'][h(i, 4) + 1] || ' order ' || i AS property_dealname,
    CAST(1000 + h(i * 3, 20) * 500 AS VARCHAR) AS property_amount,
    ['appointmentscheduled', 'qualifiedtobuy', 'contractsent', 'closedwon', 'closedwon', 'closedlost'][h(i * 5, 6) + 1] AS property_dealstage,
    CAST(epoch(TIMESTAMP '2024-01-15' + to_days(h(i * 7, 620))) AS BIGINT) * 1000 AS property_closedate,
    1000 + h(i * 11, 400) + 1 AS associated_vid
FROM range(1, 151) t(i);
COMMENT ON TABLE raw_hubspot.deals IS 'HubSpot B2B wholesale deals. property_amount is a USD string, property_closedate is epoch milliseconds.';

-- ───────────────────────────── raw_ads ─────────────────────────────

CREATE TABLE raw_ads.google_ads_campaign_stats AS
WITH base AS (
    SELECT CAST(d AS DATE) AS date, c, datediff('day', DATE '2024-01-01', d) AS dk
    FROM generate_series(TIMESTAMP '2024-01-01', TIMESTAMP '2025-09-30', INTERVAL 1 DAY) g(d) CROSS JOIN range(1, 5) r(c)
), m AS (
    SELECT *, 500 + h(dk * 7 + c, 3000) AS impressions FROM base
)
SELECT
    date,
    1000 + c AS campaign_id,
    ['Brand - Search', 'Generic - Footwear', 'Generic - Apparel', 'Shopping - All Products'][c] AS campaign_name,
    impressions,
    CAST(impressions * (2 + h(dk * 11 + c, 5)) / 100 AS INTEGER) AS clicks,
    CAST(round(CAST(impressions * (2 + h(dk * 11 + c, 5)) / 100 AS INTEGER) * (0.6 + h(dk * 13 + c, 120) / 100.0) * 1000000) AS BIGINT) AS cost_micros
FROM m ORDER BY date, campaign_id;
COMMENT ON TABLE raw_ads.google_ads_campaign_stats IS 'Google Ads daily campaign stats. cost_micros is USD × 1,000,000.';
COMMENT ON COLUMN raw_ads.google_ads_campaign_stats.cost_micros IS 'Spend in micros: divide by 1,000,000 for USD.';

CREATE TABLE raw_ads.facebook_ads_insights AS
WITH base AS (
    SELECT CAST(d AS DATE) AS date, c, datediff('day', DATE '2024-01-01', d) AS dk
    FROM generate_series(TIMESTAMP '2024-01-01', TIMESTAMP '2025-09-30', INTERVAL 1 DAY) g(d) CROSS JOIN range(1, 4) r(c)
), m AS (
    SELECT *, 800 + h(dk * 17 + c, 5000) AS impressions FROM base
)
SELECT
    date AS date_start,
    date AS date_stop,
    CAST(23850000000 + c AS VARCHAR) AS campaign_id,
    ['Prospecting - Lookalike', 'Retargeting - Cart', 'Prospecting - Interests'][c] AS campaign_name,
    impressions,
    CAST(impressions * (1 + h(dk * 19 + c, 3)) / 100 AS INTEGER) AS inline_link_clicks,
    CAST(round(CAST(impressions * (1 + h(dk * 19 + c, 3)) / 100 AS INTEGER) * (0.35 + h(dk * 23 + c, 70) / 100.0), 2) AS DECIMAL(12, 2)) AS spend,
    'EUR' AS account_currency
FROM m ORDER BY date, campaign_id;
COMMENT ON TABLE raw_ads.facebook_ads_insights IS 'Facebook Ads daily insights. spend is in the ad account currency (EUR).';

-- ───────────────────────────── raw_erp ─────────────────────────────

CREATE TABLE raw_erp.warehouses AS
SELECT * FROM (VALUES (1, 'NJ1', 'Newark DC', 'US'), (2, 'NV1', 'Reno DC', 'US'), (3, 'NL1', 'Rotterdam DC', 'NL'))
    v(id, code, name, country);
COMMENT ON TABLE raw_erp.warehouses IS 'Distribution centers from the ERP.';

CREATE TABLE raw_erp.inventory_snapshots AS
WITH base AS (
    SELECT CAST(d AS DATE) AS snapshot_date, datediff('day', DATE '2025-01-01', d) AS dk, p.sku, p.id AS pid, p.status, w.id AS warehouse_id
    FROM generate_series(TIMESTAMP '2025-01-01', TIMESTAMP '2025-09-30', INTERVAL 1 DAY) g(d)
    CROSS JOIN raw_shopify.products p CROSS JOIN raw_erp.warehouses w
)
SELECT
    snapshot_date,
    warehouse_id,
    sku,
    CASE WHEN status = 'archived' THEN 0 ELSE 20 + h(dk * 7 + pid * 13 + warehouse_id, 200) END AS qty_on_hand,
    CASE WHEN status = 'archived' THEN 0 ELSE h(dk * 11 + pid * 17 + warehouse_id, 15) END AS qty_reserved
FROM base ORDER BY snapshot_date, warehouse_id, sku;
COMMENT ON TABLE raw_erp.inventory_snapshots IS 'End-of-day stock per warehouse and SKU (2025 only). Quantities are point-in-time balances.';
