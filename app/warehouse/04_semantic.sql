-- Semantic schema: the dbt MetricFlow layer's own models (dbt/models/). Views over marts that shape facts for
-- the governed metrics (refunds per order, subscription month-ends, first replies). Hidden from the raw-SQL
-- agent's discovery tools (db.HIDDEN_SCHEMAS): they're the semantic layer's private modelling.

CREATE SCHEMA semantic;

CREATE VIEW semantic.time_spine AS
SELECT date_day FROM marts.dim_date;

CREATE VIEW semantic.customers AS
SELECT
    c.customer_key,
    c.email,
    c.first_name || ' ' || c.last_name AS customer_name,
    c.country_code,
    CASE WHEN c.is_wholesale THEN 'wholesale'
         WHEN c.customer_key IN (SELECT customer_key FROM marts.dim_subscriptions) THEN 'member'
         ELSE 'retail' END AS customer_segment,
    coalesce(c.hubspot_original_source, 'unknown') AS acquisition_source
FROM marts.dim_customers c;

CREATE VIEW semantic.products AS
SELECT product_id, product_name, category, vendor FROM marts.dim_products;

CREATE VIEW semantic.orders AS
SELECT
    o.order_id,
    o.customer_key,
    o.order_date,
    o.sales_channel,
    o.currency,
    o.cancelled_at IS NOT NULL AS is_cancelled,
    row_number() OVER (PARTITION BY o.shopify_customer_id ORDER BY o.created_at) = 1 AS is_first_order,
    CASE WHEN o.cancelled_at IS NOT NULL THEN 'cancelled'
         WHEN o.financial_status = 'refunded' THEN 'refunded'
         WHEN o.financial_status = 'partially_refunded' THEN 'partially_refunded'
         WHEN o.financial_status = 'pending' THEN 'pending'
         ELSE 'completed' END AS order_status,
    o.subtotal_usd,
    least(coalesce(r.refund_usd, 0), o.subtotal_usd) AS refund_usd
FROM marts.fct_orders o
LEFT JOIN (SELECT order_id, SUM(refund_amount_usd) AS refund_usd FROM marts.fct_refunds GROUP BY 1) r USING (order_id);

CREATE VIEW semantic.order_lines AS
SELECT l.order_line_id, l.order_id, l.product_id, l.order_date, l.quantity, l.line_amount_usd,
       o.cancelled_at IS NOT NULL AS is_cancelled
FROM marts.fct_order_lines l
JOIN marts.fct_orders o ON o.order_id = l.order_id;

-- Grain: subscription × month end, 2024-01 to 2025-09.
CREATE VIEW semantic.subscription_months AS
SELECT
    s.subscription_id || ':' || strftime(m.month_start, '%Y-%m') AS subscription_month_id,
    m.month_start AS month,
    s.subscription_id,
    s.customer_key,
    s.plan_name,
    s.billing_interval,
    CASE WHEN s.billing_interval = 'year' THEN s.plan_amount_usd / 12 ELSE s.plan_amount_usd END AS monthly_amount_usd,
    s.canceled_at IS NULL OR CAST(s.canceled_at AS DATE) > last_day(m.month_start) AS active_at_month_end,
    date_trunc('month', s.started_at) = m.month_start AS started_in_month,
    s.canceled_at IS NOT NULL AND date_trunc('month', s.canceled_at) = m.month_start AS canceled_in_month
FROM marts.dim_subscriptions s
JOIN (SELECT DISTINCT month_start FROM marts.dim_date WHERE month_start BETWEEN DATE '2024-01-01' AND DATE '2025-09-01') m
  ON m.month_start >= date_trunc('month', s.started_at)
 AND (s.canceled_at IS NULL OR m.month_start <= date_trunc('month', s.canceled_at));

CREATE VIEW semantic.tickets AS
SELECT
    t.ticket_id,
    t.customer_key,
    CAST(t.created_at AS DATE) AS created_date,
    t.priority,
    t.channel,
    t.status,
    t.subject,
    t.status IN ('solved', 'closed') AS is_solved,
    date_diff('minute', t.created_at, fr.first_reply_at) / 60.0 AS first_reply_hours,
    date_diff('minute', t.created_at, t.solved_at) / 60.0 AS resolution_hours
FROM marts.fct_tickets t
LEFT JOIN (
    SELECT ticket_id, MIN(created_at) AS first_reply_at
    FROM marts.fct_ticket_comments
    WHERE is_public AND author_role = 'agent'
    GROUP BY 1
) fr USING (ticket_id);

CREATE VIEW semantic.ad_stats AS
SELECT date || ':' || platform || ':' || campaign_id AS ad_stat_id, * FROM marts.fct_ad_stats;

CREATE VIEW semantic.inventory_snapshots AS
SELECT snapshot_date || ':' || warehouse_code || ':' || product_id AS snapshot_id, * FROM marts.fct_inventory_snapshots;
