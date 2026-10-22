-- Staging layer (dbt stg_ models): one model per source table. Renames columns, casts types,
-- converts cents/micros/epochs, and drops soft-deleted rows. No joins across sources.

CREATE SCHEMA staging;

CREATE TABLE staging.stg_finance__fx_rates AS
SELECT date, currency, rate_to_usd FROM raw_finance.fx_rates;
COMMENT ON TABLE staging.stg_finance__fx_rates IS 'Daily FX rates to USD. One row per date and currency (USD, EUR, GBP).';

CREATE TABLE staging.stg_shopify__customers AS
SELECT
    id AS customer_id,
    lower(email) AS email,
    first_name,
    last_name,
    country_code,
    created_at,
    accepts_marketing,
    tags LIKE '%wholesale%' AS is_wholesale
FROM raw_shopify.customers
WHERE NOT _fivetran_deleted;
COMMENT ON TABLE staging.stg_shopify__customers IS 'Shopify customers, excluding deleted ones. Grain: one row per Shopify customer.';
COMMENT ON COLUMN staging.stg_shopify__customers.is_wholesale IS 'Customer has the "wholesale" tag.';

CREATE TABLE staging.stg_shopify__products AS
SELECT id AS product_id, title AS product_name, product_type AS category, vendor, sku, status, created_at
FROM raw_shopify.products
WHERE NOT _fivetran_deleted;
COMMENT ON TABLE staging.stg_shopify__products IS 'Shopify products. Grain: one row per product.';

CREATE TABLE staging.stg_shopify__orders AS
SELECT
    id AS order_id,
    name AS order_number,
    customer_id,
    created_at,
    CAST(created_at AS DATE) AS order_date,
    currency,
    CAST(subtotal_price AS DECIMAL(12, 2)) AS subtotal,
    CAST(total_shipping_price AS DECIMAL(12, 2)) AS shipping,
    CAST(total_tax AS DECIMAL(12, 2)) AS tax,
    CAST(total_price AS DECIMAL(12, 2)) AS total_price,
    financial_status,
    cancelled_at IS NOT NULL AS is_cancelled,
    test AS is_test,
    source_name
FROM raw_shopify.orders
WHERE NOT _fivetran_deleted;
COMMENT ON TABLE staging.stg_shopify__orders IS 'Shopify orders with numeric amounts in the ORDER CURRENCY (not USD). Still includes test orders (is_test).';
COMMENT ON COLUMN staging.stg_shopify__orders.subtotal IS 'Line totals after discounts, order currency. Excludes shipping and tax.';
COMMENT ON COLUMN staging.stg_shopify__orders.total_price IS 'subtotal + shipping + tax, order currency.';

CREATE TABLE staging.stg_shopify__order_lines AS
SELECT
    id AS order_line_id,
    order_id,
    product_id,
    quantity,
    price AS unit_price,
    total_discount AS discount,
    quantity * price - total_discount AS line_total
FROM raw_shopify.order_lines;
COMMENT ON TABLE staging.stg_shopify__order_lines IS 'Shopify order lines. Amounts are in the order currency.';

CREATE TABLE staging.stg_shopify__refunds AS
SELECT id AS refund_id, order_id, created_at AS refunded_at, amount AS refund_amount, currency
FROM raw_shopify.refunds;
COMMENT ON TABLE staging.stg_shopify__refunds IS 'Shopify refunds in the order currency. A full refund includes shipping and tax.';

CREATE TABLE staging.stg_stripe__customers AS
SELECT id AS stripe_customer_id, lower(email) AS email, to_timestamp(created)::TIMESTAMP AS created_at
FROM raw_stripe.customers;
COMMENT ON TABLE staging.stg_stripe__customers IS 'Stripe customers (membership subscribers).';

CREATE TABLE staging.stg_stripe__subscriptions AS
SELECT
    s.id AS subscription_id,
    s.customer AS stripe_customer_id,
    s.plan AS plan_id,
    p.nickname AS plan_name,
    p."interval" AS billing_interval,
    p.amount / 100.0 AS plan_amount_usd,
    s.status,
    to_timestamp(s.start_date)::TIMESTAMP AS started_at,
    to_timestamp(s.canceled_at)::TIMESTAMP AS canceled_at
FROM raw_stripe.subscriptions s
JOIN raw_stripe.plans p ON p.id = s.plan;
COMMENT ON TABLE staging.stg_stripe__subscriptions IS 'Stripe subscriptions with plan attributes. plan_amount_usd is per billing interval (month or year).';

CREATE TABLE staging.stg_stripe__invoices AS
SELECT
    id AS invoice_id,
    subscription AS subscription_id,
    customer AS stripe_customer_id,
    amount_due / 100.0 AS amount_due_usd,
    amount_paid / 100.0 AS amount_paid_usd,
    status,
    to_timestamp(period_start)::TIMESTAMP AS period_start,
    to_timestamp(created)::TIMESTAMP AS created_at
FROM raw_stripe.invoices;
COMMENT ON TABLE staging.stg_stripe__invoices IS 'Stripe invoices in USD. status: paid or uncollectible.';

CREATE TABLE staging.stg_zendesk__users AS
SELECT id AS zendesk_user_id, lower(email) AS email, name, role, created_at FROM raw_zendesk.users;
COMMENT ON TABLE staging.stg_zendesk__users IS 'Zendesk users. role is end-user (customer) or agent (support staff).';

CREATE TABLE staging.stg_zendesk__tickets AS
SELECT
    id AS ticket_id,
    requester_id,
    assignee_id,
    subject,
    status,
    coalesce(priority, 'normal') AS priority,
    via_channel AS channel,
    created_at,
    solved_at
FROM raw_zendesk.tickets;
COMMENT ON TABLE staging.stg_zendesk__tickets IS 'Zendesk tickets. Unset priority is defaulted to normal.';

CREATE TABLE staging.stg_zendesk__ticket_comments AS
SELECT c.id AS comment_id, c.ticket_id, c.author_id, u.role AS author_role, c.public AS is_public, c.created_at
FROM raw_zendesk.ticket_comments c
LEFT JOIN raw_zendesk.users u ON u.id = c.author_id;
COMMENT ON TABLE staging.stg_zendesk__ticket_comments IS 'Ticket comments. is_public = FALSE are internal agent notes.';

CREATE TABLE staging.stg_hubspot__contacts AS
SELECT
    vid AS contact_id,
    lower(property_email) AS email,
    property_lifecyclestage AS lifecycle_stage,
    lower(property_hs_analytics_source) AS original_source,
    make_timestamp(property_createdate * 1000) AS created_at
FROM raw_hubspot.contacts
WHERE NOT _fivetran_deleted;
COMMENT ON TABLE staging.stg_hubspot__contacts IS 'HubSpot contacts, both leads and customers.';

CREATE TABLE staging.stg_hubspot__deals AS
SELECT
    deal_id,
    property_dealname AS deal_name,
    CAST(property_amount AS DECIMAL(12, 2)) AS amount_usd,
    property_dealstage AS deal_stage,
    CAST(make_timestamp(property_closedate * 1000) AS DATE) AS close_date,
    associated_vid AS contact_id
FROM raw_hubspot.deals;
COMMENT ON TABLE staging.stg_hubspot__deals IS 'HubSpot wholesale deals.';

CREATE TABLE staging.stg_ads__spend AS
SELECT
    date, 'google_ads' AS platform, CAST(campaign_id AS VARCHAR) AS campaign_id, campaign_name,
    impressions, clicks, round(cost_micros / 1e6, 2) AS cost_usd
FROM raw_ads.google_ads_campaign_stats
UNION ALL
SELECT
    f.date_start, 'facebook_ads', f.campaign_id, f.campaign_name,
    f.impressions, f.inline_link_clicks, round(f.spend * fx.rate_to_usd, 2)
FROM raw_ads.facebook_ads_insights f
JOIN raw_finance.fx_rates fx ON fx.currency = f.account_currency AND fx.date = f.date_start;
COMMENT ON TABLE staging.stg_ads__spend IS 'Daily ad platform stats unioned across Google and Facebook, cost converted to USD.';

CREATE TABLE staging.stg_erp__inventory_snapshots AS
SELECT
    s.snapshot_date,
    w.code AS warehouse_code,
    s.sku,
    s.qty_on_hand,
    s.qty_reserved
FROM raw_erp.inventory_snapshots s
JOIN raw_erp.warehouses w ON w.id = s.warehouse_id;
COMMENT ON TABLE staging.stg_erp__inventory_snapshots IS 'End-of-day inventory per warehouse and SKU (2025 only).';
