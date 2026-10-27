-- Row counts of the Model Armor and Admin Activity audit tables. They are recorded before and after a run to show
-- that the evaluation deleted nothing: every count after the run must be >= the count before it.
-- Placeholders: {P} workspace project, {A} audit dataset.
SELECT table_id, row_count
FROM `{P}.{A}.__TABLES__`
WHERE STARTS_WITH(table_id, 'modelarmor_') OR STARTS_WITH(table_id, 'cloudaudit_googleapis_com_activity')
ORDER BY table_id
