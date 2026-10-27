-- Ground truth: Model Armor sanitize operations with a BLOCK verdict, counted over calendar dates in the workspace
-- time zone (inclusive). Placeholders: {MA} Model Armor table, {TZ} workspace time zone, {start}, {end}.
-- blocked_rows matches the product KPI (row count). blocked_distinct removes duplicate sink deliveries (same
-- insertId and timestamp). blocked_rows_utc_dates is a diagnostic only: the same dates read in UTC, which is the
-- signature of a time-zone mistake.
WITH w AS (
  SELECT timestamp, insertId
  FROM {MA}
  WHERE timestamp >= TIMESTAMP_SUB(TIMESTAMP('{start}', '{TZ}'), INTERVAL 1 DAY)
    AND timestamp < TIMESTAMP_ADD(TIMESTAMP(DATE_ADD(DATE '{end}', INTERVAL 1 DAY), '{TZ}'), INTERVAL 1 DAY)
    AND IFNULL(jsonpayload_v1_sanitizeoperationlogentry.sanitizationresult.sanitizationverdict, '') LIKE '%BLOCK%'
)
SELECT
  COUNTIF(DATE(timestamp, '{TZ}') BETWEEN DATE '{start}' AND DATE '{end}') AS blocked_rows,
  COUNT(DISTINCT IF(DATE(timestamp, '{TZ}') BETWEEN DATE '{start}' AND DATE '{end}',
                    CONCAT(IFNULL(insertId, ''), '|', CAST(timestamp AS STRING)), NULL)) AS blocked_distinct,
  COUNTIF(DATE(timestamp) BETWEEN DATE '{start}' AND DATE '{end}') AS blocked_rows_utc_dates
FROM w
