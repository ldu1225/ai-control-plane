-- Ground truth (TR-05, C-02): excluded spend (SKU or service matching the workspace's excluded SKU patterns, for
-- example Code Assist / Duet AI) over calendar dates in the workspace time zone (inclusive). These figures must NEVER
-- appear in an analyst answer, row or suggestion. Placeholders: {BILL}, {TZ}, {start}, {end}, {EXCLUDED_RE}.
SELECT
  COUNT(*) AS excluded_rows,
  ROUND(SUM(cost), 2) AS excluded_cost,
  ARRAY_TO_STRING(ARRAY_AGG(DISTINCT sku.description IGNORE NULLS LIMIT 10), ' ; ') AS excluded_skus
FROM {BILL}
WHERE DATE(usage_start_time, '{TZ}') BETWEEN DATE '{start}' AND DATE '{end}'
  AND REGEXP_CONTAINS(LOWER(CONCAT(IFNULL(sku.description, ''), ' | ', IFNULL(service.description, ''))),
                      r'{EXCLUDED_RE}')
