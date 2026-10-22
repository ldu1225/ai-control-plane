-- Ground truth: Model Armor BLOCK verdicts split by operation side, over calendar dates in the workspace time zone
-- (inclusive). Placeholders: {MA}, {TZ}, {start}, {end}.
-- Operation types contain PROMPT or RESPONSE (for example SANITIZE_USER_PROMPT / SANITIZE_MODEL_RESPONSE).
-- search_result_blocked counts blocked grounding (web search) results, which the product reports separately from
-- prompts that people typed.
WITH w AS (
  SELECT
    UPPER(IFNULL(jsonpayload_v1_sanitizeoperationlogentry.operationtype, '')) AS op,
    IFNULL(REGEXP_CONTAINS(jsonpayload_v1_sanitizeoperationlogentry.sanitizationinput.text,
           r"^\{'uri': 'https://vertexaisearch\.cloud\.google\.com/grounding-api-redirect/"), FALSE) AS search_result
  FROM {MA}
  WHERE timestamp >= TIMESTAMP('{start}', '{TZ}')
    AND timestamp < TIMESTAMP(DATE_ADD(DATE '{end}', INTERVAL 1 DAY), '{TZ}')
    AND IFNULL(jsonpayload_v1_sanitizeoperationlogentry.sanitizationresult.sanitizationverdict, '') LIKE '%BLOCK%'
)
SELECT
  COUNT(*) AS blocked_rows,
  COUNTIF(op LIKE '%PROMPT%') AS prompt_blocked,
  COUNTIF(op LIKE '%RESPONSE%') AS response_blocked,
  COUNTIF(NOT (op LIKE '%PROMPT%' OR op LIKE '%RESPONSE%')) AS other_blocked,
  COUNTIF(search_result) AS search_result_blocked
FROM w
