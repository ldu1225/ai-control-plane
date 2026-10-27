-- Ground truth (bait validity, HB-03): billing export coverage. pre_export_month is the calendar month before the
-- first usage row. HB-03 asks about that month, so pre_export_rows must be 0 for the bait to be valid.
-- Placeholders: {BILL}, {TZ}.
WITH r AS (
  SELECT MIN(usage_start_time) AS first_usage, MAX(usage_start_time) AS last_usage FROM {BILL}
)
SELECT
  FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', first_usage, '{TZ}') AS first_usage_local,
  FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', last_usage, '{TZ}') AS last_usage_local,
  FORMAT_DATE('%Y-%m', DATE_SUB(DATE_TRUNC(DATE(first_usage, '{TZ}'), MONTH), INTERVAL 1 MONTH)) AS pre_export_month,
  FORMAT_DATE('%B %Y', DATE_SUB(DATE_TRUNC(DATE(first_usage, '{TZ}'), MONTH), INTERVAL 1 MONTH))
    AS pre_export_month_label,
  (SELECT COUNT(*) FROM {BILL} b
   WHERE DATE_TRUNC(DATE(b.usage_start_time, '{TZ}'), MONTH)
         = DATE_SUB(DATE_TRUNC(DATE(r.first_usage, '{TZ}'), MONTH), INTERVAL 1 MONTH)) AS pre_export_rows
FROM r
