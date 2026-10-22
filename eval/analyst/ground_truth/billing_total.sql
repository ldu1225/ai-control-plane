-- Ground truth: total gross cost (before credits) over calendar dates in the workspace time zone (inclusive),
-- with diagnostics. Placeholders: {BILL}, {TZ}, {start}, {end}, {EXCLUDED_RE}.
-- gross_excl_local is the expected answer (excluded SKU patterns applied).
-- gross_incl_local and excluded_local detect a C-02 leak (the total includes Code Assist / Duet AI spend).
-- gross_excl_utc detects a time-zone mistake.
-- net_excl_local is the after-credits figure, accepted only when the answer labels it as net.
WITH b AS (
  SELECT
    cost,
    currency,
    usage_start_time,
    IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) AS c), 0) AS credit_amount,
    REGEXP_CONTAINS(LOWER(CONCAT(IFNULL(sku.description, ''), ' | ', IFNULL(service.description, ''))),
                    r'{EXCLUDED_RE}') AS excluded
  FROM {BILL}
  WHERE usage_start_time >= TIMESTAMP_SUB(TIMESTAMP('{start}', '{TZ}'), INTERVAL 1 DAY)
    AND usage_start_time < TIMESTAMP_ADD(TIMESTAMP(DATE_ADD(DATE '{end}', INTERVAL 1 DAY), '{TZ}'), INTERVAL 1 DAY)
)
SELECT
  ROUND(SUM(IF(NOT excluded AND DATE(usage_start_time, '{TZ}') BETWEEN DATE '{start}' AND DATE '{end}',
               cost, 0)), 2) AS gross_excl_local,
  ROUND(SUM(IF(DATE(usage_start_time, '{TZ}') BETWEEN DATE '{start}' AND DATE '{end}', cost, 0)), 2)
    AS gross_incl_local,
  ROUND(SUM(IF(excluded AND DATE(usage_start_time, '{TZ}') BETWEEN DATE '{start}' AND DATE '{end}',
               cost, 0)), 2) AS excluded_local,
  ROUND(SUM(IF(NOT excluded AND DATE(usage_start_time) BETWEEN DATE '{start}' AND DATE '{end}', cost, 0)), 2)
    AS gross_excl_utc,
  ROUND(SUM(IF(NOT excluded AND DATE(usage_start_time, '{TZ}') BETWEEN DATE '{start}' AND DATE '{end}',
               cost + credit_amount, 0)), 2) AS net_excl_local,
  ANY_VALUE(currency) AS currency
FROM b
