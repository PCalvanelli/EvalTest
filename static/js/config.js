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
  {text: 'Which ad platform had the lowest cost per click in Q2 2025, and what was it?'},
  {text: 'How many urgent tickets came in during 2025, and what was our average first response time on them?'},
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
  // Judge-only: a correct answer can label the two groups many ways (TRUE/FALSE, 'first'/'repeat'), so exact row matching would be unfair.
  {text: "How does average order value compare between customers' first orders and their repeat orders in 2024?"},
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
   expected_sql: "WITH o AS (SELECT customer_key, order_date, row_number() OVER (PARTITION BY customer_key ORDER BY created_at) AS n FROM marts.fct_orders), f AS (SELECT customer_key, max(order_date) FILTER (WHERE n = 1) AS first_date, max(order_date) FILTER (WHERE n = 2) AS second_date FROM o GROUP BY 1) SELECT AVG(CASE WHEN second_date <= first_date + INTERVAL 30 DAY THEN 1.0 ELSE 0.0 END) AS share FROM f WHERE second_date IS NOT NULL"},
  {text: "What is the median number of days between a customer's first and second order?",
   expected_sql: "WITH o AS (SELECT order_date, lead(order_date) OVER (PARTITION BY customer_key ORDER BY created_at) AS next_date, row_number() OVER (PARTITION BY customer_key ORDER BY created_at) AS n FROM marts.fct_orders) SELECT median(next_date - order_date) AS median_days FROM o WHERE n = 1 AND next_date IS NOT NULL"},
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
export const DEFAULT_RUBRIC = `PASS only if:
- The query directly answers the question asked (correct grain, filters, time window, and aggregation).
- Business terms follow the company's definitions: revenue is merchandise only (no shipping/tax), excludes cancelled
  orders, and net revenue deducts refunds; MRR counts subscriptions active at month end with annual plans / 12;
  active customers have a non-cancelled order in the period; first response = first PUBLIC agent reply.
- Gross revenue is merchandise after discounts, excluding cancelled orders, before refunds; units sold and
  AOV also exclude cancelled orders; churned memberships are those canceled during the period.
- It uses current sources (no deprecated tables) and treats inventory snapshots as point-in-time.
- The returned rows are plausible for the question (not empty unless that is the correct answer).
- Ratios are fractions.
FAIL if the query answers a different question, misses a filter, or the explanation contradicts the query.`;

export const cellName = (cell) => `${modelName(cell.model)} · ${comboLabel({harness: cell.harness_on, context: cell.context})}`;
export const comboLabel = (c) => `${CONTEXT_SHORT[c.context] ?? c.context} · ${c.harness ? 'harness' : 'no harness'}`;
export const modelSlot = (model) => Math.max(0, MODELS.findIndex((m) => m.id === model)) + 1;  // fixed: color follows the model

// The results being shown on the Run tab: a live matrix, or one loaded from History.
//   cells: [{id, model, harness_on, context, run_id?}], runs: {runNo: {cellId: report}}, errors: {cellId: msg}
//   mlflow: {matrix_id, matrix, cells: {cellId: url}, cell_runs: {cellId: runId}} when tracked
export const freshState = (extra = {}) => ({cells: [], repeats: 1, currentRun: 0, runs: {}, errors: {}, prompts: [],
  selected: null, selectedRun: null, mlflow: null, loadedFrom: null, ...extra});
export const app = {state: freshState(), pricing: {}, config: null};
