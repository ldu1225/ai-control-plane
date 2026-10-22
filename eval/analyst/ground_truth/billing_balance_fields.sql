-- Ground truth (bait validity, HB-01): the billing export has no field that holds a remaining credit balance.
-- Placeholders: {BP} billing project, {BDS} billing dataset, {BTABLE} billing table.
-- balance_like_fields must be 0. credits_applied_total shows what the export does contain (credits consumed).
SELECT
  (SELECT COUNT(*) FROM `{BP}.{BDS}.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS`
   WHERE table_name = '{BTABLE}' AND REGEXP_CONTAINS(LOWER(field_path), r'balance|remaining')) AS balance_like_fields,
  (SELECT STRING_AGG(DISTINCT field_path, ', ') FROM `{BP}.{BDS}.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS`
   WHERE table_name = '{BTABLE}' AND STARTS_WITH(LOWER(field_path), 'credits')) AS credit_fields,
  (SELECT ROUND(SUM(c.amount), 2) FROM `{BP}.{BDS}.{BTABLE}`, UNNEST(credits) AS c) AS credits_applied_total
