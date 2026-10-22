// Constants shared across views, plus the single mutable app state object.

export const MODELS = [
  {id: 'claude-opus-5-5', name: 'Opus 5.5'},
  {id: 'claude-sonnet-5-5', name: 'Sonnet 5.5', checked: true},
  {id: 'claude-haiku-5-5', name: 'Haiku 5.5'},
];
export const modelName = (id) => MODELS.find((m) => m.id === id)?.name ?? id;
export const CONTEXTS = [
  {id: 'raw', name: 'Raw SQL'},
  {id: 'metricflow', name: 'dbt MetricFlow'},
  {id: 'malloy', name: 'Malloy'},
];
// Short labels, including retired contexts so older matrices in History still read correctly.
export const CONTEXT_SHORT = {raw: 'Raw SQL', metricflow: 'MetricFlow', malloy: 'Malloy',
  glossary: 'Glossary (retired)', glossary_search: 'Glossary search (retired)', semantic: 'YAML semantic (retired)'};
// Column order of the matrix grid: context (retired ones last, for older matrices), then harness.
export const COMBOS = Object.keys(CONTEXT_SHORT).flatMap((context) => [false, true].map((harness) => ({context, harness})));
export const cellId = (model, harness, context) => `${model}|${harness ? 'harness' : 'no-harness'}|${context}`;

// Business-phrased questions. Expected SQL encodes the governed metric definitions (dbt/models/metrics.yml),
// so it is the company's "right answer", not just any plausible one.
export const SAMPLE_PROMPTS = [
  {text: 'What was our net revenue in 2024?',
   expected_sql: "SELECT SUM(o.subtotal_usd - least(coalesce(r.refund_usd, 0), o.subtotal_usd)) FROM marts.fct_orders o LEFT JOIN (SELECT order_id, SUM(refund_amount_usd) AS refund_usd FROM marts.fct_refunds GROUP BY 1) r USING (order_id) WHERE o.cancelled_at IS NULL AND o.order_date BETWEEN '2024-01-01' AND '2024-12-31'"},
  {text: 'What was MRR at the end of June 2025, by plan?',
   expected_sql: "SELECT plan_name, SUM(CASE WHEN billing_interval = 'year' THEN plan_amount_usd / 12 ELSE plan_amount_usd END) FROM marts.dim_subscriptions WHERE started_at < DATE '2025-07-01' AND (canceled_at IS NULL OR CAST(canceled_at AS DATE) > DATE '2025-06-30') GROUP BY 1"},
  {text: 'How many active customers did we have in Q3 2025?',
   expected_sql: "SELECT COUNT(DISTINCT customer_key) FROM marts.fct_orders WHERE cancelled_at IS NULL AND order_date BETWEEN '2025-07-01' AND '2025-09-30'"},
  // match: 'numbers' compares only the numeric columns: the label (platform name, first/repeat) may be written many ways.
  {text: 'Which ad platform had the lowest cost per click in Q2 2025, and what was it?', match: 'numbers',
   expected_sql: "SELECT platform, SUM(cost_usd) / SUM(clicks) AS cost_per_click FROM marts.fct_ad_stats WHERE date BETWEEN '2025-04-01' AND '2025-06-30' GROUP BY 1 ORDER BY 2 LIMIT 1"},
  {text: 'How many urgent tickets came in during 2025, and what was our average first response time on them?',
   expected_sql: "SELECT COUNT(*), AVG(date_diff('minute', t.created_at, fr.m) / 60.0) FROM marts.fct_tickets t LEFT JOIN (SELECT ticket_id, MIN(created_at) m FROM marts.fct_ticket_comments WHERE is_public AND author_role = 'agent' GROUP BY 1) fr USING (ticket_id) WHERE priority = 'urgent' AND t.created_at >= '2025-01-01'"},
  {text: 'How many Footwear units did we have available to sell at the end of September 2025?',
   expected_sql: "SELECT SUM(i.qty_on_hand - i.qty_reserved) FROM marts.fct_inventory_snapshots i JOIN marts.dim_products p USING (product_id) WHERE i.snapshot_date = '2025-09-30' AND p.category = 'Footwear'"},
  {text: 'How many customers have both placed an order and opened a support ticket?',
   expected_sql: 'SELECT COUNT(DISTINCT o.customer_key) FROM marts.fct_orders o JOIN marts.fct_tickets t USING (customer_key)'},
  {text: 'What was gross revenue by sales channel in Q2 2025?',
   expected_sql: "SELECT sales_channel, SUM(subtotal_usd) FROM marts.fct_orders WHERE cancelled_at IS NULL AND order_date BETWEEN '2025-04-01' AND '2025-06-30' GROUP BY 1"},
  {text: 'How many memberships churned in 2025 so far?',
   expected_sql: "SELECT COUNT(*) FROM marts.dim_subscriptions WHERE canceled_at >= '2025-01-01' AND canceled_at < '2025-10-01'"},
  {text: 'What were our top 3 products by units sold in 2025?',
   expected_sql: "SELECT p.product_name, SUM(l.quantity) FROM marts.fct_order_lines l JOIN marts.fct_orders o USING (order_id) JOIN marts.dim_products p USING (product_id) WHERE o.cancelled_at IS NULL AND l.order_date >= '2025-01-01' GROUP BY 1 ORDER BY 2 DESC LIMIT 3"},
  {text: "How does average order value compare between customers' first orders and their repeat orders in 2024?", match: 'numbers',
   expected_sql: "WITH o AS (SELECT o.*, coalesce(r.refund_usd, 0) AS refund_usd, row_number() OVER (PARTITION BY o.shopify_customer_id ORDER BY o.created_at) = 1 AS is_first FROM marts.fct_orders o LEFT JOIN (SELECT order_id, SUM(refund_amount_usd) AS refund_usd FROM marts.fct_refunds GROUP BY 1) r USING (order_id)) SELECT is_first, SUM(subtotal_usd - least(refund_usd, subtotal_usd)) / COUNT(*) FROM o WHERE cancelled_at IS NULL AND year(order_date) = 2024 GROUP BY 1"},
  {text: 'How many units did each warehouse have on hand at the end of Q2 2025?',
   expected_sql: "SELECT warehouse_code, SUM(qty_on_hand) FROM marts.fct_inventory_snapshots WHERE snapshot_date = '2025-06-30' GROUP BY 1"},
  // Harder set: the expected SQL needs CTEs and window functions (LAG/LEAD, running and framed aggregates, ranking).
  {text: "What was the month-over-month growth rate of net revenue for each month of 2025? Return month and growth as a fraction (January compares with December 2024).",
   expected_sql: "WITH o AS (SELECT o.*, o.subtotal_usd - least(coalesce(r.refund_usd, 0), o.subtotal_usd) AS net_usd FROM marts.fct_orders o LEFT JOIN (SELECT order_id, SUM(refund_amount_usd) AS refund_usd FROM marts.fct_refunds GROUP BY 1) r USING (order_id) WHERE o.cancelled_at IS NULL), m AS (SELECT date_trunc('month', order_date) AS month, SUM(net_usd) AS net_revenue FROM o GROUP BY 1) SELECT month, net_revenue / lag(net_revenue) OVER (ORDER BY month) - 1 AS mom_growth FROM m QUALIFY month >= DATE '2025-01-01' ORDER BY month"},
  {text: "What was our cumulative (year-to-date) net revenue at the end of each month of 2025? Return month and cumulative net revenue.",
   expected_sql: "WITH o AS (SELECT o.*, o.subtotal_usd - least(coalesce(r.refund_usd, 0), o.subtotal_usd) AS net_usd FROM marts.fct_orders o LEFT JOIN (SELECT order_id, SUM(refund_amount_usd) AS refund_usd FROM marts.fct_refunds GROUP BY 1) r USING (order_id) WHERE o.cancelled_at IS NULL), m AS (SELECT date_trunc('month', order_date) AS month, SUM(net_usd) AS net_revenue FROM o WHERE order_date >= DATE '2025-01-01' GROUP BY 1) SELECT month, SUM(net_revenue) OVER (ORDER BY month) AS cumulative_net_revenue FROM m ORDER BY month"},
  {text: "What was the best-selling product by units sold in each product category in 2025? Return category, product name and units; list every product tied for first.",
   expected_sql: "WITH s AS (SELECT p.category, p.product_name, SUM(l.quantity) AS units FROM marts.fct_order_lines l JOIN marts.fct_orders o USING (order_id) JOIN marts.dim_products p USING (product_id) WHERE o.cancelled_at IS NULL AND l.order_date >= DATE '2025-01-01' GROUP BY 1, 2) SELECT category, product_name, units FROM s QUALIFY rank() OVER (PARTITION BY category ORDER BY units DESC) = 1"},
  {text: "Of customers who placed a second order, what share placed it within 30 days of their first? Return the share as a fraction.",
   expected_sql: "WITH o AS (SELECT customer_key, order_date, row_number() OVER (PARTITION BY customer_key ORDER BY created_at) AS n FROM marts.fct_orders WHERE customer_key IS NOT NULL), f AS (SELECT customer_key, max(order_date) FILTER (WHERE n = 1) AS first_date, max(order_date) FILTER (WHERE n = 2) AS second_date FROM o GROUP BY 1) SELECT AVG(CASE WHEN second_date <= first_date + INTERVAL 30 DAY THEN 1.0 ELSE 0.0 END) AS share FROM f WHERE second_date IS NOT NULL"},
  {text: "What is the median number of days between a customer's first and second order?",
   expected_sql: "WITH o AS (SELECT order_date, lead(order_date) OVER (PARTITION BY customer_key ORDER BY created_at) AS next_date, row_number() OVER (PARTITION BY customer_key ORDER BY created_at) AS n FROM marts.fct_orders WHERE customer_key IS NOT NULL) SELECT median(next_date - order_date) AS median_days FROM o WHERE n = 1 AND next_date IS NOT NULL"},
  {text: "How did MRR change month over month in 2025? Return month, MRR at month end, and the change from the prior month.",
   expected_sql: "WITH m AS (SELECT date_day AS month FROM marts.dim_date WHERE day(date_day) = 1 AND date_day BETWEEN DATE '2024-12-01' AND DATE '2025-09-01'), mrr AS (SELECT m.month, SUM(CASE WHEN s.billing_interval = 'year' THEN s.plan_amount_usd / 12 ELSE s.plan_amount_usd END) AS mrr FROM m JOIN marts.dim_subscriptions s ON s.started_at < m.month + INTERVAL 1 MONTH AND (s.canceled_at IS NULL OR CAST(s.canceled_at AS DATE) > last_day(m.month)) GROUP BY 1) SELECT month, mrr, mrr - lag(mrr) OVER (ORDER BY month) AS mrr_change FROM mrr QUALIFY month >= DATE '2025-01-01' ORDER BY month"},
  {text: "What was monthly ad spend in 2025, with a trailing 3-month average? Return month, spend and the 3-month average.",
   expected_sql: "WITH m AS (SELECT date_trunc('month', date) AS month, SUM(cost_usd) AS spend FROM marts.fct_ad_stats GROUP BY 1) SELECT month, spend, avg(spend) OVER (ORDER BY month ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS rolling_3m_avg FROM m QUALIFY month >= DATE '2025-01-01' ORDER BY month"},
  {text: "Who are our top 5 customers by lifetime net revenue, and what share of total net revenue does each represent? Return email, net revenue and share as a fraction.",
   expected_sql: "WITH o AS (SELECT o.*, o.subtotal_usd - least(coalesce(r.refund_usd, 0), o.subtotal_usd) AS net_usd FROM marts.fct_orders o LEFT JOIN (SELECT order_id, SUM(refund_amount_usd) AS refund_usd FROM marts.fct_refunds GROUP BY 1) r USING (order_id) WHERE o.cancelled_at IS NULL), c AS (SELECT customer_key, SUM(net_usd) AS net_revenue FROM o GROUP BY 1) SELECT d.email, c.net_revenue, c.net_revenue / SUM(c.net_revenue) OVER () AS share_of_total FROM c JOIN marts.dim_customers d USING (customer_key) QUALIFY row_number() OVER (ORDER BY c.net_revenue DESC) <= 5 ORDER BY c.net_revenue DESC"},
  {text: "For tickets created in 2025, what share got a first public agent reply within 4 hours, by priority? Return priority and the share as a fraction.",
   expected_sql: "WITH fr AS (SELECT ticket_id, MIN(created_at) AS first_reply_at FROM marts.fct_ticket_comments WHERE is_public AND author_role = 'agent' GROUP BY 1) SELECT t.priority, AVG(CASE WHEN fr.first_reply_at <= t.created_at + INTERVAL 4 HOUR THEN 1.0 ELSE 0.0 END) AS share_within_4h FROM marts.fct_tickets t LEFT JOIN fr USING (ticket_id) WHERE t.created_at >= '2025-01-01' GROUP BY 1"},
  {text: "On which day in Q3 2025 did each warehouse have the fewest units available to sell, and how many? Return warehouse, date and units.",
   expected_sql: "WITH a AS (SELECT warehouse_code, snapshot_date, SUM(qty_on_hand - qty_reserved) AS available FROM marts.fct_inventory_snapshots WHERE snapshot_date BETWEEN DATE '2025-07-01' AND DATE '2025-09-30' GROUP BY 1, 2) SELECT warehouse_code, snapshot_date, available FROM a QUALIFY row_number() OVER (PARTITION BY warehouse_code ORDER BY available, snapshot_date) = 1"},
];
// Held-out benchmark: written and verified before the semantic layers and harness were tuned on SAMPLE_PROMPTS,
// so a score here shows whether those improvements generalize. Don't tune against it.
export const HOLDOUT_PROMPTS = [
  {text: "By how much did net revenue grow from Q3 2024 to Q4 2024? Return the growth rate as a fraction.",
   expected_sql: "WITH o AS (SELECT o.*, o.subtotal_usd - least(coalesce(r.refund_usd, 0), o.subtotal_usd) AS net_usd FROM marts.fct_orders o LEFT JOIN (SELECT order_id, SUM(refund_amount_usd) AS refund_usd FROM marts.fct_refunds GROUP BY 1) r USING (order_id) WHERE o.cancelled_at IS NULL), q AS (SELECT date_trunc('quarter', order_date) AS quarter, SUM(net_usd) AS net_revenue FROM o GROUP BY 1) SELECT net_revenue / lag(net_revenue) OVER (ORDER BY quarter) - 1 AS growth FROM q QUALIFY quarter = DATE '2024-10-01'"},
  {text: "For each sales channel, which product category had the most product revenue in 2025? Return sales channel, category and product revenue; list every category tied for first.",
   expected_sql: "WITH s AS (SELECT o.sales_channel, p.category, SUM(l.line_amount_usd) AS product_revenue FROM marts.fct_order_lines l JOIN marts.fct_orders o USING (order_id) JOIN marts.dim_products p USING (product_id) WHERE o.cancelled_at IS NULL AND l.order_date >= DATE '2025-01-01' GROUP BY 1, 2) SELECT sales_channel, category, product_revenue FROM s QUALIFY rank() OVER (PARTITION BY sales_channel ORDER BY product_revenue DESC) = 1"},
  {text: "For tickets created in 2025, what share were solved within 24 hours of creation, by channel? Return channel and the share as a fraction.",
   expected_sql: "SELECT channel, AVG(CASE WHEN solved_at <= created_at + INTERVAL 24 HOUR THEN 1.0 ELSE 0.0 END) AS share_solved_24h FROM marts.fct_tickets WHERE created_at >= '2025-01-01' GROUP BY 1"},
  {text: "What is the median number of days between a customer's second and third order?",
   expected_sql: "WITH o AS (SELECT order_date, lead(order_date) OVER (PARTITION BY customer_key ORDER BY created_at) AS next_date, row_number() OVER (PARTITION BY customer_key ORDER BY created_at) AS n FROM marts.fct_orders WHERE customer_key IS NOT NULL) SELECT median(next_date - order_date) AS median_days FROM o WHERE n = 2 AND next_date IS NOT NULL"},
  {text: "How many customers have both held a membership subscription and opened a support ticket?",
   expected_sql: "SELECT COUNT(DISTINCT s.customer_key) FROM marts.dim_subscriptions s JOIN marts.fct_tickets t USING (customer_key)"},
  {text: "What was the running total of new membership subscriptions through each month of 2025 (January to September)? Return month and the running total since January 2025.",
   expected_sql: "WITH m AS (SELECT date_day AS month FROM marts.dim_date WHERE day(date_day) = 1 AND date_day BETWEEN DATE '2025-01-01' AND DATE '2025-09-01'), n AS (SELECT CAST(date_trunc('month', started_at) AS DATE) AS month, COUNT(*) AS new_subs FROM marts.dim_subscriptions GROUP BY 1) SELECT m.month, SUM(coalesce(n.new_subs, 0)) OVER (ORDER BY m.month) AS running_new_subscriptions FROM m LEFT JOIN n USING (month)"},
  {text: "What was the trailing 3-month cost per click for each month of 2025 (total spend over the 3 months divided by total clicks over the 3 months)? Return month and the trailing CPC.",
   expected_sql: "WITH m AS (SELECT date_trunc('month', date) AS month, SUM(cost_usd) AS spend, SUM(clicks) AS clicks FROM marts.fct_ad_stats GROUP BY 1) SELECT month, SUM(spend) OVER w / SUM(clicks) OVER w AS trailing_3m_cpc FROM m WINDOW w AS (ORDER BY month ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) QUALIFY month >= DATE '2025-01-01'"},
  {text: "Which 2 customers placed the most orders in 2025, and what share of all orders placed in 2025 does each account for? Return email, order count and share as a fraction.",
   expected_sql: "WITH c AS (SELECT customer_key, COUNT(*) AS orders FROM marts.fct_orders WHERE order_date >= DATE '2025-01-01' GROUP BY 1), t AS (SELECT SUM(orders) AS total FROM c) SELECT d.email, c.orders, c.orders / t.total AS share FROM c JOIN marts.dim_customers d USING (customer_key) CROSS JOIN t QUALIFY rank() OVER (ORDER BY c.orders DESC) <= 2"},
  {text: "For each warehouse, which product had the most units on hand on 2025-09-30? Return warehouse, product name and units on hand; list every product tied for first.",
   expected_sql: "WITH s AS (SELECT i.warehouse_code, p.product_name, SUM(i.qty_on_hand) AS units FROM marts.fct_inventory_snapshots i JOIN marts.dim_products p USING (product_id) WHERE i.snapshot_date = DATE '2025-09-30' GROUP BY 1, 2) SELECT warehouse_code, product_name, units FROM s QUALIFY rank() OVER (PARTITION BY warehouse_code ORDER BY units DESC) = 1"},
  {text: "How did the number of active memberships at quarter end change quarter over quarter in 2025 (Q1 to Q3)? Return the quarter's first month, active subscriptions at quarter end, and the change from the prior quarter end.",
   expected_sql: "WITH q AS (SELECT date_day AS quarter, last_day(date_day + INTERVAL 2 MONTH) AS quarter_end FROM marts.dim_date WHERE day(date_day) = 1 AND month(date_day) IN (1, 4, 7, 10) AND date_day BETWEEN DATE '2024-10-01' AND DATE '2025-07-01'), a AS (SELECT q.quarter, COUNT(*) AS active FROM q JOIN marts.dim_subscriptions s ON CAST(s.started_at AS DATE) <= q.quarter_end AND (s.canceled_at IS NULL OR CAST(s.canceled_at AS DATE) > q.quarter_end) GROUP BY 1) SELECT quarter, active, active - lag(active) OVER (ORDER BY quarter) AS change FROM a QUALIFY quarter >= DATE '2025-01-01'"},
];
export const PROMPT_SETS = {benchmark: SAMPLE_PROMPTS, holdout: HOLDOUT_PROMPTS};

export const DEFAULT_RUBRIC = `Company definitions. Each applies only to the metrics it names:
- Net revenue: merchandise subtotal after discounts (no shipping or tax), minus refunds capped at the order
  subtotal, excluding cancelled orders.
- Gross revenue: merchandise subtotal after discounts, excluding cancelled orders, before refunds.
- Units sold, product revenue and average order value: exclude cancelled orders.
- Active customers: distinct customers with a non-cancelled order in the period.
- Orders placed, and a customer's first, second or third order: every order counts, cancelled ones included.
- MRR: subscriptions active at month end; annual plans count as price / 12. Point-in-time, never summed across months.
- Churned memberships: subscriptions canceled during the period.
- First response: the first PUBLIC agent reply (internal notes don't count).
- Inventory: snapshots are point-in-time; use the snapshot date asked for, never a sum across dates.
- Sources: current tables only, never deprecated ones.
- Ratios are fractions (0.25, not 25%).

PASS only if the query answers the question asked (grain, filters, time window, aggregation), uses these definitions
wherever the question involves them, returns plausible rows, and the explanation matches what the query does.
FAIL if it answers a different question, misses a filter a definition requires, or the explanation contradicts the query.`;

export const cellName = (cell) => `${modelName(cell.model)} · ${comboLabel({harness: cell.harness_on, context: cell.context})}`;
export const comboLabel = (c) => `${CONTEXT_SHORT[c.context] ?? c.context} · ${c.harness ? 'harness' : 'no harness'}`;
export const modelSlot = (model) => Math.max(0, MODELS.findIndex((m) => m.id === model)) + 1;  // fixed: color follows the model

// The results being shown on the Run tab: a live matrix, or one loaded from History.
//   cells: [{id, model, harness_on, context, run_id?}], runs: {runNo: {cellId: report}}, errors: {cellId: msg}
//   mlflow: {matrix_id, matrix, cells: {cellId: url}, cell_runs: {cellId: runId}} when tracked
export const freshState = (extra = {}) => ({cells: [], repeats: 1, currentRun: 0, runs: {}, errors: {}, prompts: [],
  selected: null, selectedRun: null, mlflow: null, loadedFrom: null, ...extra});
export const app = {state: freshState(), pricing: {}, config: null};
