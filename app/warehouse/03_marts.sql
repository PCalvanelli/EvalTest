-- Marts layer (dbt fct_/dim_ models): cleaned entities and events, money converted to USD, and a
-- conformed customer_key. Deliberately NO business metrics (revenue, MRR, active, churn, response times,
-- segments): those definitions live in the semantic layer (app/semantic/warehouse.yaml).

CREATE SCHEMA marts;

CREATE TABLE marts.dim_date AS
SELECT
    CAST(d AS DATE) AS date_day,
    year(d) AS year,
    quarter(d) AS quarter,
    month(d) AS month,
    monthname(d) AS month_name,
    CAST(date_trunc('month', d) AS DATE) AS month_start,
    CAST(date_trunc('week', d) AS DATE) AS week_start,
    dayname(d) AS day_name,
    isodow(d) IN (6, 7) AS is_weekend
FROM generate_series(TIMESTAMP '2023-01-01', TIMESTAMP '2025-12-31', INTERVAL 1 DAY) g(d);
COMMENT ON TABLE marts.dim_date IS 'Calendar dimension, 2023-01-01 to 2025-12-31.';

CREATE TABLE marts.dim_customers AS
WITH emails AS (
    SELECT email FROM staging.stg_shopify__customers
    UNION SELECT email FROM staging.stg_stripe__customers
    UNION SELECT email FROM staging.stg_zendesk__users WHERE role = 'end-user'
)
SELECT
    row_number() OVER (ORDER BY e.email) AS customer_key,
    e.email,
    sc.customer_id AS shopify_customer_id,
    st.stripe_customer_id,
    zu.zendesk_user_id,
    hc.contact_id AS hubspot_contact_id,
    sc.first_name,
    sc.last_name,
    sc.country_code,
    hc.original_source AS hubspot_original_source,
    coalesce(sc.is_wholesale, FALSE) AS is_wholesale,
    least(sc.created_at, st.created_at, zu.created_at) AS first_seen_at
FROM emails e
LEFT JOIN staging.stg_shopify__customers sc ON sc.email = e.email
LEFT JOIN staging.stg_stripe__customers st ON st.email = e.email
LEFT JOIN staging.stg_zendesk__users zu ON zu.email = e.email AND zu.role = 'end-user'
LEFT JOIN staging.stg_hubspot__contacts hc ON hc.email = e.email;
COMMENT ON TABLE marts.dim_customers IS 'People resolved across Shopify, Stripe, Zendesk and HubSpot by email. Grain: one row per person.';
COMMENT ON COLUMN marts.dim_customers.customer_key IS 'Surrogate key used by every fact table.';
COMMENT ON COLUMN marts.dim_customers.is_wholesale IS 'Shopify customer carries the wholesale tag.';
COMMENT ON COLUMN marts.dim_customers.hubspot_original_source IS 'HubSpot hs_analytics_source, lower-cased. NULL when no HubSpot contact matched.';
COMMENT ON COLUMN marts.dim_customers.country_code IS 'From Shopify. NULL for people without a Shopify account.';

CREATE TABLE marts.dim_products AS
SELECT p.product_id, p.product_name, p.category, p.vendor, p.sku, p.status, cur.price AS list_price_usd
FROM staging.stg_shopify__products p
LEFT JOIN (
    SELECT product_id, arg_max(price, changed_at) AS price FROM raw_shopify.product_price_changes GROUP BY 1
) cur USING (product_id);
COMMENT ON TABLE marts.dim_products IS 'Products with their current list price. Historical prices: dim_products_history.';

CREATE TABLE marts.dim_products_history AS
SELECT
    product_id,
    price AS list_price_usd,
    changed_at AS valid_from,
    lead(changed_at) OVER (PARTITION BY product_id ORDER BY changed_at) AS valid_to,
    lead(changed_at) OVER (PARTITION BY product_id ORDER BY changed_at) IS NULL AS is_current
FROM raw_shopify.product_price_changes;
COMMENT ON TABLE marts.dim_products_history IS 'Type-2 history of list prices. A row is valid for valid_from <= t < valid_to (valid_to NULL = current).';

CREATE TABLE marts.dim_subscriptions AS
SELECT
    s.subscription_id,
    c.customer_key,
    s.stripe_customer_id,
    s.plan_id,
    s.plan_name,
    s.billing_interval,
    CAST(s.plan_amount_usd AS DECIMAL(10, 2)) AS plan_amount_usd,
    s.status,
    s.started_at,
    s.canceled_at
FROM staging.stg_stripe__subscriptions s
LEFT JOIN marts.dim_customers c ON c.stripe_customer_id = s.stripe_customer_id;
COMMENT ON TABLE marts.dim_subscriptions IS 'Stripe membership subscriptions. Grain: one row per subscription.';
COMMENT ON COLUMN marts.dim_subscriptions.plan_amount_usd IS 'Price per billing_interval (month or year), USD.';
COMMENT ON COLUMN marts.dim_subscriptions.status IS 'Stripe status: active, past_due, canceled.';

CREATE TABLE marts.fct_orders AS
SELECT
    o.order_id,
    o.order_number,
    c.customer_key,
    o.customer_id AS shopify_customer_id,
    o.created_at,
    o.order_date,
    o.currency,
    fx.rate_to_usd AS fx_rate_to_usd,
    CAST(round(o.subtotal * fx.rate_to_usd, 2) AS DECIMAL(12, 2)) AS subtotal_usd,
    CAST(round(o.shipping * fx.rate_to_usd, 2) AS DECIMAL(12, 2)) AS shipping_usd,
    CAST(round(o.tax * fx.rate_to_usd, 2) AS DECIMAL(12, 2)) AS tax_usd,
    CAST(round(o.total_price * fx.rate_to_usd, 2) AS DECIMAL(12, 2)) AS total_usd,
    o.financial_status,
    CASE WHEN o.is_cancelled THEN o.created_at + INTERVAL 2 HOUR END AS cancelled_at,
    o.source_name AS sales_channel
FROM staging.stg_shopify__orders o
JOIN staging.stg_finance__fx_rates fx ON fx.currency = o.currency AND fx.date = o.order_date
LEFT JOIN marts.dim_customers c ON c.shopify_customer_id = o.customer_id
WHERE NOT o.is_test;
COMMENT ON TABLE marts.fct_orders IS 'Shopify orders excluding test orders, amounts converted to USD at the order-date rate. Grain: one row per order.';
COMMENT ON COLUMN marts.fct_orders.subtotal_usd IS 'Line amounts after discounts, USD. Excludes shipping and tax.';
COMMENT ON COLUMN marts.fct_orders.total_usd IS 'subtotal + shipping + tax, USD.';
COMMENT ON COLUMN marts.fct_orders.financial_status IS 'Shopify financial status: paid, pending, refunded, partially_refunded, voided.';
COMMENT ON COLUMN marts.fct_orders.cancelled_at IS 'Set when the order was cancelled in Shopify.';
COMMENT ON COLUMN marts.fct_orders.customer_key IS 'NULL when the Shopify customer was deleted.';
COMMENT ON COLUMN marts.fct_orders.sales_channel IS 'Shopify source_name: web, pos, mobile_app.';

CREATE TABLE marts.fct_order_lines AS
SELECT
    l.order_line_id,
    l.order_id,
    o.order_date,
    o.customer_key,
    l.product_id,
    l.quantity,
    CAST(round(l.unit_price * o.fx_rate_to_usd, 2) AS DECIMAL(12, 2)) AS unit_price_usd,
    CAST(round(l.discount * o.fx_rate_to_usd, 2) AS DECIMAL(12, 2)) AS discount_usd,
    CAST(round(l.line_total * o.fx_rate_to_usd, 2) AS DECIMAL(12, 2)) AS line_amount_usd
FROM staging.stg_shopify__order_lines l
JOIN marts.fct_orders o USING (order_id);
COMMENT ON TABLE marts.fct_order_lines IS 'Order line items for non-test orders, USD. Grain: one row per line.';
COMMENT ON COLUMN marts.fct_order_lines.line_amount_usd IS 'quantity × unit price − discount, USD.';

CREATE TABLE marts.fct_refunds AS
SELECT
    r.refund_id,
    r.order_id,
    r.refunded_at,
    CAST(round(r.refund_amount * o.fx_rate_to_usd, 2) AS DECIMAL(12, 2)) AS refund_amount_usd
FROM staging.stg_shopify__refunds r
JOIN marts.fct_orders o USING (order_id);
COMMENT ON TABLE marts.fct_refunds IS 'Shopify refunds converted to USD at the original order rate. A full refund also covers shipping and tax.';

CREATE TABLE marts.fct_invoices AS
SELECT i.invoice_id, i.subscription_id, s.customer_key, i.period_start, i.amount_due_usd, i.amount_paid_usd, i.status, i.created_at
FROM staging.stg_stripe__invoices i
LEFT JOIN marts.dim_subscriptions s USING (subscription_id);
COMMENT ON TABLE marts.fct_invoices IS 'Stripe subscription invoices, USD. One row per billing period.';

CREATE TABLE marts.fct_tickets AS
SELECT
    t.ticket_id,
    c.customer_key,
    t.created_at,
    t.subject,
    t.status,
    t.priority,
    t.channel,
    t.assignee_id,
    t.solved_at
FROM staging.stg_zendesk__tickets t
LEFT JOIN marts.dim_customers c ON c.zendesk_user_id = t.requester_id;
COMMENT ON TABLE marts.fct_tickets IS 'Zendesk support tickets. Grain: one row per ticket.';
COMMENT ON COLUMN marts.fct_tickets.status IS 'Zendesk status: new, open, pending, solved, closed.';
COMMENT ON COLUMN marts.fct_tickets.priority IS 'low, normal, high, urgent (unset defaults to normal).';
COMMENT ON COLUMN marts.fct_tickets.channel IS 'email, web, chat, phone.';

CREATE TABLE marts.fct_ticket_comments AS
SELECT comment_id, ticket_id, author_id, author_role, is_public, created_at
FROM staging.stg_zendesk__ticket_comments;
COMMENT ON TABLE marts.fct_ticket_comments IS 'Zendesk ticket comments. Grain: one row per comment.';
COMMENT ON COLUMN marts.fct_ticket_comments.is_public IS 'FALSE for internal notes that the requester never sees.';
COMMENT ON COLUMN marts.fct_ticket_comments.author_role IS 'end-user or agent.';

CREATE TABLE marts.fct_ad_stats AS
SELECT date, platform, campaign_id, campaign_name, impressions, clicks, cost_usd FROM staging.stg_ads__spend;
COMMENT ON TABLE marts.fct_ad_stats IS 'Daily ad platform stats per campaign, cost in USD. platform: google_ads, facebook_ads.';

CREATE TABLE marts.fct_inventory_snapshots AS
SELECT s.snapshot_date, s.warehouse_code, p.product_id, s.sku, s.qty_on_hand, s.qty_reserved
FROM staging.stg_erp__inventory_snapshots s
JOIN marts.dim_products p USING (sku);
COMMENT ON TABLE marts.fct_inventory_snapshots IS 'End-of-day ERP stock balances per warehouse and product (2025 only). Grain: date × warehouse × product.';
COMMENT ON COLUMN marts.fct_inventory_snapshots.qty_reserved IS 'Units allocated to open orders.';
COMMENT ON COLUMN marts.fct_inventory_snapshots.warehouse_code IS 'NJ1 (Newark), NV1 (Reno), NL1 (Rotterdam).';

CREATE TABLE marts.fct_deals AS
SELECT d.deal_id, c.customer_key, d.deal_name, d.amount_usd, d.deal_stage, d.close_date
FROM staging.stg_hubspot__deals d
LEFT JOIN staging.stg_hubspot__contacts hc USING (contact_id)
LEFT JOIN marts.dim_customers c ON c.email = hc.email;
COMMENT ON TABLE marts.fct_deals IS 'HubSpot wholesale deals, USD.';
COMMENT ON COLUMN marts.fct_deals.deal_stage IS 'HubSpot stage: appointmentscheduled, qualifiedtobuy, contractsent, closedwon, closedlost.';

CREATE TABLE marts.fct_orders_legacy AS
SELECT order_id, customer_id, order_date, total_price AS revenue, currency, financial_status
FROM staging.stg_shopify__orders;
COMMENT ON TABLE marts.fct_orders_legacy IS 'DEPRECATED (2024-11): replaced by marts.fct_orders. Amounts are in the original currency and test orders are included.';
