-- Ground truth (bait validity, HB-02): Antigravity token telemetry (the inference_response log sink) is absent from
-- the audit dataset. inference_tables must be 0 for the bait to be valid. If the table exists, the case is reported
-- as invalid for that workspace, because the analyst should then answer from the data.
-- Placeholders: {P}, {A}.
SELECT COUNT(*) AS inference_tables
FROM `{P}.{A}.INFORMATION_SCHEMA.TABLES`
WHERE STARTS_WITH(table_name, 'businessaicode_googleapis_com_inference_response')
