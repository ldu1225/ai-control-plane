-- Ground truth: billing labels ranked by gross cost (before credits), over calendar dates in the workspace time
-- zone (inclusive). Placeholders: {BILL} billing export table, {label} = service.description | sku.description,
-- {TZ}, {start}, {end}, {n}, {EXCLUDED_RE}.
-- C-02: rows whose SKU or service description matches the workspace's excluded SKU patterns are always excluded.
-- These patterns always include the product's mandatory 'code assist' and 'duet ai'.
SELECT
  {label} AS label,
  ROUND(SUM(cost), 2) AS gross_cost,
  ANY_VALUE(currency) AS currency
FROM {BILL}
WHERE DATE(usage_start_time, '{TZ}') BETWEEN DATE '{start}' AND DATE '{end}'
  AND NOT REGEXP_CONTAINS(LOWER(CONCAT(IFNULL(sku.description, ''), ' | ', IFNULL(service.description, ''))),
                          r'{EXCLUDED_RE}')
GROUP BY label
ORDER BY gross_cost DESC, label
LIMIT {n}
