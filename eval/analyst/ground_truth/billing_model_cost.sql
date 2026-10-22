-- Ground truth: gross cost of two models' SKUs over calendar dates in the workspace time zone (inclusive).
-- Placeholders: {BILL}, {TZ}, {start}, {end}, {EXCLUDED_RE}, {model_a_re}, {model_b_re}.
-- The harness derives the model regexes from the case parameters model_a / model_b. For example, gemini-3.8-flash
-- matches 'Gemini 3.8 Flash Global Text Input'. Lite and Image variants of the same tier are separate models and are
-- excluded. Matched SKU names are returned for audit.
WITH b AS (
  SELECT LOWER(IFNULL(sku.description, '')) AS sku, sku.description AS sku_raw, cost
  FROM {BILL}
  WHERE DATE(usage_start_time, '{TZ}') BETWEEN DATE '{start}' AND DATE '{end}'
    AND NOT REGEXP_CONTAINS(LOWER(CONCAT(IFNULL(sku.description, ''), ' | ', IFNULL(service.description, ''))),
                            r'{EXCLUDED_RE}')
), t AS (
  SELECT *,
    REGEXP_CONTAINS(sku, r'{model_a_re}') AND NOT REGEXP_CONTAINS(sku, r'{model_a_re}[ -](lite|image)') AS is_a,
    REGEXP_CONTAINS(sku, r'{model_b_re}') AND NOT REGEXP_CONTAINS(sku, r'{model_b_re}[ -](lite|image)') AS is_b
  FROM b
)
SELECT
  ROUND(SUM(IF(is_a, cost, 0)), 2) AS model_a_cost,
  ROUND(SUM(IF(is_b, cost, 0)), 2) AS model_b_cost,
  COUNTIF(is_a) AS model_a_rows,
  COUNTIF(is_b) AS model_b_rows,
  ARRAY_TO_STRING(ARRAY_AGG(DISTINCT IF(is_a OR is_b, sku_raw, NULL) IGNORE NULLS LIMIT 20), ' ; ') AS matched_skus
FROM t
